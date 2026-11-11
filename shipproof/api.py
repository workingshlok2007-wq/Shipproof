"""FastAPI service: the agent engine. The dashboard is only a client of this API.

Run:  uvicorn shipproof.api:buildApp --factory --host 127.0.0.1 --port 8000
Env:  SHIPPROOF_API_TOKEN (required, >= 24 chars), SHIPPROOF_DATA_FILE, SHIPPROOF_CONFUSION_THRESHOLD,
      SHIPPROOF_AUDIT_BUDGET_SECONDS (default 90), SHIPPROOF_LLM_MODE (live|record|replay), SHIPPROOF_LLM_CACHE,
      SHIPPROOF_DEMO=1 (offline fixture site/repo/inbox), SHIPPROOF_DASHBOARD_ORIGIN, GEMINI_API_KEY, GEMINI_MODEL,
      GITHUB_TOKEN, GITHUB_WRITE_TOKEN, GMAIL_TOKEN_FILE, SHIPPROOF_CHROMIUM_PATH
"""

from __future__ import annotations

import asyncio
import hmac
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, ValidationError

from shipproof.agent.graph import AuditDependencies, newCheckpointer, runAudit
from shipproof.agent.project import LinkTarget, Project
from shipproof.agent.store import ConflictError, EventBus, NotFoundError, ProjectStore, StoreError
from shipproof.remediation import FileChange, RemediationKind, RemediationStatus, fillLicenseTemplate
from shipproof.tools.githubAudit import GitHubError
from shipproof.tools.githubWrite import GitHubWriter
from shipproof.llm import BudgetedLlm, LlmClient, LlmConfigError, LlmRuntime, ReplayLlm
from shipproof.models import (
    Actor,
    ActorKind,
    ApprovalState,
    CharterAmendment,
    ModelError,
    ProofCheck,
    Role,
    SourceRef,
    WorkbenchCheck,
    addCharterClause,
    applyAmendment,
    confirmCoverage,
    promoteProofCheck,
    reconfirmProofCheck,
    transitionPacket,
    utcNow,
)

MIN_TOKEN_CHARS = 24
WS_AUTH_TIMEOUT_SECONDS = 5
OWNER = Actor(actorId="owner", kind=ActorKind.HUMAN, role=Role.OWNER)

ToolboxFactory = Callable[[], ContextManager[dict[str, BaseTool]]]
LlmFactory = Callable[[], tuple[Optional[LlmClient], Optional[str]]]
WriterFactory = Callable[[], GitHubWriter]


@dataclass(frozen=True)
class Settings:
    apiToken: str = field(repr=False)
    dataFile: Optional[Path] = None
    confusionThreshold: int = 15
    dashboardOrigin: str = "http://localhost:3000"
    budgetSeconds: int = 90
    llmMode: str = "live"
    llmCacheFile: Path = Path("shipproof-llm-cache.json")
    demo: bool = False

    def __post_init__(self) -> None:
        if len(self.apiToken) < MIN_TOKEN_CHARS:
            raise ValueError(f"SHIPPROOF_API_TOKEN must be at least {MIN_TOKEN_CHARS} characters")
        if not 1 <= self.confusionThreshold <= 100:
            raise ValueError("SHIPPROOF_CONFUSION_THRESHOLD must be in [1, 100]")
        if not 15 <= self.budgetSeconds <= 600:
            raise ValueError("SHIPPROOF_AUDIT_BUDGET_SECONDS must be in [15, 600]")
        if self.llmMode not in ("live", "record", "replay"):
            raise ValueError("SHIPPROOF_LLM_MODE must be live, record or replay")

    @classmethod
    def fromEnvironment(cls) -> Settings:
        try:
            threshold = int(os.environ.get("SHIPPROOF_CONFUSION_THRESHOLD", "15"))
            budget = int(os.environ.get("SHIPPROOF_AUDIT_BUDGET_SECONDS", "90"))
        except ValueError as error:
            raise ValueError("SHIPPROOF_CONFUSION_THRESHOLD and SHIPPROOF_AUDIT_BUDGET_SECONDS must be integers") from error
        dataFile = os.environ.get("SHIPPROOF_DATA_FILE", "shipproof-data.json")
        return cls(apiToken=os.environ.get("SHIPPROOF_API_TOKEN", ""), dataFile=Path(dataFile) if dataFile else None,
                   confusionThreshold=threshold, dashboardOrigin=os.environ.get("SHIPPROOF_DASHBOARD_ORIGIN", "http://localhost:3000"),
                   budgetSeconds=budget, llmMode=os.environ.get("SHIPPROOF_LLM_MODE", "live"),
                   llmCacheFile=Path(os.environ.get("SHIPPROOF_LLM_CACHE", "shipproof-llm-cache.json")),
                   demo=os.environ.get("SHIPPROOF_DEMO") == "1")


def makeToolbox(demoBase: Optional[str] = None) -> ToolboxFactory:
    @contextmanager
    def toolbox() -> Iterator[dict[str, BaseTool]]:
        yield from _toolbox(demoBase)
    return toolbox


def _toolbox(demoBase: Optional[str]) -> Iterator[dict[str, BaseTool]]:
    """Built inside the audit worker thread: Playwright's sync API is bound to the thread that started it."""
    from shipproof.tools.agentTools import buildTools
    from shipproof.tools.githubAudit import GitHubClient
    from shipproof.tools.gmailIngest import GmailError, buildGmailService, loadGmailCredentials
    from shipproof.tools.linkAudit import LinkAuditor

    gmailService = None
    if os.environ.get("GMAIL_TOKEN_FILE"):
        try:
            gmailService = buildGmailService(loadGmailCredentials(interactive=False))
        except GmailError:
            gmailService = None  # ingest records the inbox as NOT_CHECKED with the reason
    github = GitHubClient(token=os.environ.get("GITHUB_TOKEN") or None)
    try:
        with LinkAuditor(allowedOrigins=(demoBase,) if demoBase else ()) as auditor:
            tools = {tool.name: tool for tool in buildTools(github, auditor, gmailService)}
            if demoBase:
                from shipproof.demo import demoTools
                tools.update({tool.name: tool for tool in demoTools(demoBase, tools["auditRepository"])})
            yield tools
    finally:
        github.close()


defaultToolbox = makeToolbox()


def defaultWriter() -> GitHubWriter:
    token = os.environ.get("GITHUB_WRITE_TOKEN", "")
    if not token:
        raise GitHubError("GITHUB_WRITE_TOKEN is not set on the engine; applying fixes needs a token with Contents and "
                          "Pull requests write access to this repository")
    return GitHubWriter(token)


# ---------------------------------------------------------------- request bodies


class ProjectBody(BaseModel):
    projectId: str
    repo: str
    proposalText: str
    links: list[LinkTarget] = []
    rulePages: list[str] = []
    emailQuery: Optional[str] = None


class ClauseBody(BaseModel):
    clauseId: str
    text: str
    sourceRef: SourceRef


class AmendmentBody(BaseModel):
    amendmentId: str
    clauseId: str
    newText: str
    sourceRef: SourceRef


class ProofBody(BaseModel):
    checkId: str
    clauseId: str
    interpretation: str


class ReconfirmBody(BaseModel):
    interpretation: str


class WorkbenchBody(BaseModel):
    checkId: str
    tier: int = Field(3, ge=3)
    label: str
    description: Optional[str] = None


def _validationDetail(error: ValidationError) -> list[dict[str, Any]]:
    return [{"loc": [str(p) for p in e["loc"]], "msg": e["msg"]} for e in error.errors(include_url=False)]


class AnswerBody(BaseModel):
    questionId: str = Field(max_length=64)
    value: str = Field(min_length=1, max_length=64)


class AcceptBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ApplyBody(BaseModel):
    licenseKey: Optional[str] = None
    copyrightHolder: Optional[str] = Field(None, max_length=100)


def createApp(settings: Settings, toolboxFactory: Optional[ToolboxFactory] = None, llmFactory: Optional[LlmFactory] = None,
              writerFactory: WriterFactory = defaultWriter) -> FastAPI:
    """toolboxFactory/llmFactory default to the real tools and the startup-resolved Gemini model (tests inject fakes)."""
    store = ProjectStore(settings.dataFile)
    bus = EventBus()
    runtime = LlmRuntime()
    checkpointer = newCheckpointer()
    demoState: dict[str, Any] = {}

    def runtimeLlm() -> tuple[Optional[LlmClient], Optional[str]]:
        if settings.llmMode == "replay":
            return ReplayLlm(settings.llmCacheFile, "replay"), None
        if runtime.client is None:
            return None, f"model {runtime.status}: {runtime.reason or 'still resolving'}"
        if settings.llmMode == "record":
            return ReplayLlm(settings.llmCacheFile, "record", runtime.client), None
        return runtime.client, None

    resolveLlm: LlmFactory = llmFactory or runtimeLlm

    def modelLabel(llm: Optional[LlmClient]) -> Optional[str]:
        if llm is None:
            return None
        if isinstance(llm, ReplayLlm):
            return f"{llm.mode}" + (f" via {runtime.model}" if llm.mode == "record" and runtime.model else " (saved answers)")
        config = getattr(llm, "config", None)
        return getattr(config, "model", None) or type(llm).__name__

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        if llmFactory is None and settings.llmMode != "replay":
            threading.Thread(target=runtime.resolve, name="llm-resolve", daemon=True).start()
        if settings.demo:
            from shipproof.demo import DEMO_PROJECT_ID, DemoSiteServer, demoPort, demoProject
            server = DemoSiteServer(demoPort())
            demoState["server"], demoState["base"] = server, server.base
            try:
                store.getProject(DEMO_PROJECT_ID)
            except NotFoundError:
                store.createProject(demoProject(server.base))
        try:
            yield
        finally:
            if "server" in demoState:
                demoState["server"].close()

    def currentToolbox() -> ContextManager[dict[str, BaseTool]]:
        if toolboxFactory is not None:
            return toolboxFactory()
        return makeToolbox(demoState.get("base"))()

    app = FastAPI(title="ShipProof", version="0.6.0", lifespan=lifespan)
    app.state.store, app.state.bus, app.state.runtime = store, bus, runtime
    app.add_middleware(CORSMiddleware, allow_origins=[settings.dashboardOrigin], allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(NotFoundError)
    async def notFound(_: Request, error: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, error: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(ModelError)
    async def modelRule(_: Request, error: ModelError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(ValidationError)
    async def invalid(_: Request, error: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": _validationDetail(error)})

    @app.exception_handler(StoreError)
    async def storeFailure(_: Request, error: StoreError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(error)})

    def isValidToken(candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode("utf-8"), settings.apiToken.encode("utf-8"))

    def requireHuman(authorization: str = Header(default="")) -> Actor:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or not isValidToken(token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})
        return OWNER

    # ---------------------------------------------------------------- projects

    @app.get("/health")
    def health() -> dict[str, Any]:
        llm = {"status": "injected"} if llmFactory is not None else runtime.snapshot()
        return {"status": "ok", "llm": llm, "llmMode": settings.llmMode, "demo": settings.demo,
                "budgetSeconds": settings.budgetSeconds}

    @app.post("/projects", status_code=201)
    def createProject(body: ProjectBody, _: Actor = Depends(requireHuman)) -> dict[str, Any]:
        project = Project(projectId=body.projectId, repo=body.repo, proposalText=body.proposalText, links=tuple(body.links),
                          rulePages=tuple(body.rulePages), emailQuery=body.emailQuery)
        return store.createProject(project).model_dump(mode="json")

    @app.get("/projects")
    def listProjects(_: Actor = Depends(requireHuman)) -> dict[str, list[str]]:
        return {"projectIds": store.listProjectIds()}

    @app.get("/projects/{projectId}")
    def getProject(projectId: str, _: Actor = Depends(requireHuman)) -> dict[str, Any]:
        return store.getProject(projectId).model_dump(mode="json")

    @app.post("/projects/{projectId}/clauses", status_code=201)
    def addClause(projectId: str, body: ClauseBody, actor: Actor = Depends(requireHuman)) -> dict[str, Any]:
        return store.updateProject(projectId, lambda p: p.model_copy(update={
            "charter": addCharterClause(p.charter, body.clauseId, body.text, body.sourceRef, actor)})).model_dump(mode="json")

    @app.post("/projects/{projectId}/amendments", status_code=201)
    def amend(projectId: str, body: AmendmentBody, actor: Actor = Depends(requireHuman)) -> dict[str, Any]:
        amendment = CharterAmendment(amendmentId=body.amendmentId, clauseId=body.clauseId, newText=body.newText,
                                     sourceRef=body.sourceRef, submittedBy=actor, submittedAt=utcNow())

        def change(project: Project) -> Project:
            charter, checks = applyAmendment(project.charter, project.proofChecks, amendment)
            return project.model_copy(update={"charter": charter, "proofChecks": checks})
        return store.updateProject(projectId, change).model_dump(mode="json")

    @app.post("/projects/{projectId}/proof-checks", status_code=201)
    def addProof(projectId: str, body: ProofBody, actor: Actor = Depends(requireHuman)) -> dict[str, Any]:
        def change(project: Project) -> Project:
            if any(c.checkId == body.checkId for c in project.proofChecks):
                raise ConflictError(f"proof check {body.checkId} exists")
            check = ProofCheck(checkId=body.checkId, clauseId=body.clauseId, interpretation=body.interpretation, authoredBy=actor,
                               clauseVersion=project.charter.currentClause(body.clauseId).version)
            return project.model_copy(update={"proofChecks": project.proofChecks + (check,)})
        return store.updateProject(projectId, change).model_dump(mode="json")

    def _replaceCheck(projectId: str, checkId: str, transform: Callable[[ProofCheck, Project], ProofCheck]) -> dict[str, Any]:
        def change(project: Project) -> Project:
            matches = [c for c in project.proofChecks if c.checkId == checkId]
            if not matches:
                raise NotFoundError(f"proof check {checkId} not found")
            updated = transform(matches[0], project)
            return project.model_copy(update={"proofChecks": tuple(updated if c.checkId == checkId else c for c in project.proofChecks)})
        return store.updateProject(projectId, change).model_dump(mode="json")

    @app.post("/projects/{projectId}/proof-checks/{checkId}/reconfirm")
    def reconfirm(projectId: str, checkId: str, body: ReconfirmBody, actor: Actor = Depends(requireHuman)) -> dict[str, Any]:
        return _replaceCheck(projectId, checkId, lambda c, p: reconfirmProofCheck(c, p.charter, actor, body.interpretation))

    @app.post("/projects/{projectId}/proof-checks/{checkId}/promote")
    def promote(projectId: str, checkId: str, actor: Actor = Depends(requireHuman)) -> dict[str, Any]:
        return _replaceCheck(projectId, checkId, lambda c, p: promoteProofCheck(c, p.charter, actor))

    @app.post("/projects/{projectId}/workbench", status_code=201)
    def addWorkbench(projectId: str, body: WorkbenchBody, actor: Actor = Depends(requireHuman)) -> dict[str, Any]:
        def change(project: Project) -> Project:
            if any(c.checkId == body.checkId for c in project.workbenchChecks):
                raise ConflictError(f"workbench check {body.checkId} exists")
            check = WorkbenchCheck(checkId=body.checkId, tier=body.tier, label=body.label, description=body.description, authoredBy=actor)
            return project.model_copy(update={"workbenchChecks": project.workbenchChecks + (check,)})
        return store.updateProject(projectId, change).model_dump(mode="json")

    # ---------------------------------------------------------------- audit

    def launch(project: Project, packetId: str, startedAt: Any, resume: Optional[dict[str, Any]] = None) -> None:
        def emit(kind: str, data: dict[str, Any]) -> None:
            bus.publish(packetId, kind, data)

        def worker() -> None:
            try:
                llm, llmReason = resolveLlm()
                deadline = time.monotonic() + settings.budgetSeconds  # a fresh budget per run; time waiting for you is free
                label = modelLabel(llm)
                with currentToolbox() as tools:
                    deps = AuditDependencies(
                        tools=tools, llm=BudgetedLlm(llm, deadline) if llm is not None else None, llmUnavailableReason=llmReason,
                        confusionThreshold=settings.confusionThreshold, emit=emit, budgetSeconds=settings.budgetSeconds,
                        deadline=deadline, previousPacket=store.previousPacket(project.projectId, packetId),
                        checkpointer=checkpointer, llmModel=label)
                    outcome = runAudit(project, packetId, deps, startedAt, resume)
                if outcome.paused:
                    store.pauseAudit(packetId, outcome.summary)
                    emit("paused", {"questionId": outcome.question.questionId if outcome.question else None})
                    return
                assert outcome.packet is not None
                store.completeAudit(project.projectId, outcome.packet, outcome.summary, outcome.proofChecks,
                                    outcome.startProject.proofChecks, outcome.decisions)
                emit("done", {"approvalState": outcome.packet.approvalState.value, "confusionIndex": outcome.summary.confusionIndex,
                              "halted": outcome.summary.halted})
            except Exception as error:  # every failure ends at the human boundary with a CRITICAL flag
                failed = store.failAudit(project.projectId, packetId, f"{type(error).__name__}: {error}")
                emit("error", {"message": f"{type(error).__name__}: {error}"[:500]})
                emit("done", {"approvalState": failed.approvalState.value})

        threading.Thread(target=worker, name=f"audit-{packetId}", daemon=True).start()

    @app.post("/projects/{projectId}/trigger-audit", status_code=202)
    def triggerAudit(projectId: str, _: Actor = Depends(requireHuman)) -> dict[str, str]:
        project, packetId = store.beginAudit(projectId)
        bus.publish(packetId, "stage", {"stage": "queued"})
        launch(project, packetId, utcNow())
        return {"packetId": packetId}

    @app.post("/packets/{packetId}/answers", status_code=202)
    def answerQuestion(packetId: str, body: AnswerBody, actor: Actor = Depends(requireHuman)) -> dict[str, str]:
        """A human answer resumes the paused audit from where it stopped."""
        summary = store.resumeAudit(packetId, body.questionId, body.value, actor)
        bus.publish(packetId, "answer", {"questionId": body.questionId, "value": body.value})
        launch(store.getProject(summary.projectId), packetId, summary.startedAt,
               resume={"value": body.value, "actor": actor.model_dump(mode="json")})
        return {"packetId": packetId, "status": "resumed"}

    @app.post("/packets/{packetId}/flags/{flagId}/accept")
    def acceptRisk(packetId: str, flagId: str, body: AcceptBody, actor: Actor = Depends(requireHuman)) -> dict[str, Any]:
        """Explicit, recorded acceptance of one risk; remembered for later audits of the same project."""
        packet = store.acceptRisk(packetId, flagId, actor, body.reason)
        bus.publish(packetId, "riskAccepted", {"flagId": flagId})
        return packet.model_dump(mode="json")

    @app.get("/packets/{packetId}")
    def getPacket(packetId: str, _: Actor = Depends(requireHuman)) -> dict[str, Any]:
        summary = store.getSummary(packetId)
        return {"packet": store.getPacket(packetId).model_dump(mode="json"),
                "summary": summary.model_dump(mode="json") if summary else None}

    @app.get("/packets/{packetId}/events")
    def packetEventHistory(packetId: str, after: int = 0, _: Actor = Depends(requireHuman)) -> dict[str, Any]:
        """Polling fallback for clients whose proxy cannot carry WebSockets. Events are kept in memory only."""
        if after < 0:
            raise HTTPException(422, "after must be >= 0")
        store.getPacket(packetId)
        events = bus.history(packetId, after)
        return {"events": events, "next": after + len(events)}

    def _requirePending(packetId: str) -> None:
        if store.getPacket(packetId).approvalState is not ApprovalState.PENDING_HUMAN_APPROVAL:
            raise ConflictError("packet is not awaiting human approval")

    @app.post("/packets/{packetId}/coverage/confirm")
    def confirm(packetId: str, actor: Actor = Depends(requireHuman)) -> dict[str, Any]:
        _requirePending(packetId)
        return store.updatePacket(packetId, lambda p: p.model_validate(
            {**p.model_dump(), "ledger": confirmCoverage(p.ledger, actor)})).model_dump(mode="json")

    @app.post("/packets/{packetId}/approve")
    def approve(packetId: str, actor: Actor = Depends(requireHuman)) -> dict[str, Any]:
        packet = store.updatePacket(packetId, lambda p: transitionPacket(p, ApprovalState.APPROVED, actor))
        bus.publish(packetId, "decision", {"approvalState": packet.approvalState.value})
        return packet.model_dump(mode="json")

    @app.post("/packets/{packetId}/reject")
    def reject(packetId: str, actor: Actor = Depends(requireHuman)) -> dict[str, Any]:
        packet = store.updatePacket(packetId, lambda p: transitionPacket(p, ApprovalState.REJECTED, actor))
        bus.publish(packetId, "decision", {"approvalState": packet.approvalState.value})
        return packet.model_dump(mode="json")

    @app.post("/packets/{packetId}/remediations/{remediationId}/apply")
    def applyRemediation(packetId: str, remediationId: str, body: ApplyBody, _: Actor = Depends(requireHuman)) -> dict[str, Any]:
        """Human-triggered: opens a pull request on a new branch. The agent never merges."""
        remediation = store.getRemediation(packetId, remediationId)
        if not remediation.applicable:
            raise ConflictError("this remediation is advice only and cannot be applied automatically")
        project = store.getProject(store.getSummary(packetId).projectId)  # type: ignore[union-attr]
        store.beginApply(packetId, remediationId)
        try:
            writer = writerFactory()
            try:
                changes = remediation.changes
                if remediation.kind is RemediationKind.ADD_LICENSE:
                    if not body.licenseKey or not body.copyrightHolder:
                        raise ConflictError("choose a license and a copyright holder")
                    template = writer.licenseTemplate(body.licenseKey)
                    changes = (FileChange(path="LICENSE", newContent=fillLicenseTemplate(template, utcNow().year, body.copyrightHolder)),)
                prBody = "\n\n".join((remediation.rationale, *remediation.advice,
                                        f"Drafted by ShipProof from audit `{packetId}` ({remediation.draftedFrom}). Review before merging."))
                url = writer.openPullRequest(project.repo, remediationId, remediation.title, prBody, changes)
            finally:
                writer.close()
        except GitHubError as error:
            store.releaseApply(packetId, remediationId)
            raise HTTPException(502, str(error)) from error
        except BaseException:
            store.releaseApply(packetId, remediationId)
            raise
        applied = store.finishRemediation(packetId, remediationId, RemediationStatus.APPLIED, url)
        bus.publish(packetId, "remediationApplied", {"remediationId": remediationId, "pullRequestUrl": url})
        return applied.model_dump(mode="json")

    @app.post("/packets/{packetId}/remediations/{remediationId}/dismiss")
    def dismissRemediation(packetId: str, remediationId: str, _: Actor = Depends(requireHuman)) -> dict[str, Any]:
        store.beginApply(packetId, remediationId)
        return store.finishRemediation(packetId, remediationId, RemediationStatus.DISMISSED).model_dump(mode="json")

    @app.websocket("/ws/packets/{packetId}")
    async def packetEvents(websocket: WebSocket, packetId: str) -> None:
        """Auth by first message {"token": "..."} so the token never appears in URLs or access logs."""
        await websocket.accept()
        try:
            message = await asyncio.wait_for(websocket.receive_json(), timeout=WS_AUTH_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
            await websocket.close(code=4401)
            return
        token = message.get("token") if isinstance(message, dict) else None
        if not isinstance(token, str) or not isValidToken(token):
            await websocket.close(code=4401)
            return
        try:
            store.getPacket(packetId)
        except NotFoundError:
            await websocket.close(code=4404)
            return
        queue, history = bus.subscribe(packetId)
        try:
            for event in history:
                await websocket.send_json(event)
            while True:
                await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass  # client closed the stream; normal termination
        finally:
            bus.unsubscribe(packetId, queue)

    return app


def buildApp() -> FastAPI:
    """uvicorn factory: fails at startup with a clear message if configuration is invalid."""
    return createApp(Settings.fromEnvironment())
