"""FastAPI service: the agent engine. The dashboard is only a client of this API.

Run:  uvicorn shipproof.api:buildApp --factory --host 127.0.0.1 --port 8000
Env:  SHIPPROOF_API_TOKEN (required, >= 24 chars), SHIPPROOF_DATA_FILE, SHIPPROOF_CONFUSION_THRESHOLD,
      SHIPPROOF_DASHBOARD_ORIGIN, GEMINI_API_KEY, GEMINI_MODEL, GITHUB_TOKEN, GMAIL_TOKEN_FILE, SHIPPROOF_CHROMIUM_PATH
"""

from __future__ import annotations

import asyncio
import hmac
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, ValidationError

from shipproof.agent.graph import AuditDependencies, runAudit
from shipproof.agent.project import LinkTarget, Project
from shipproof.agent.store import ConflictError, EventBus, NotFoundError, ProjectStore, StoreError
from shipproof.llm import GeminiClient, GeminiConfig, LlmClient, LlmConfigError
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


@dataclass(frozen=True)
class Settings:
    apiToken: str = field(repr=False)
    dataFile: Optional[Path] = None
    confusionThreshold: int = 15
    dashboardOrigin: str = "http://localhost:3000"

    def __post_init__(self) -> None:
        if len(self.apiToken) < MIN_TOKEN_CHARS:
            raise ValueError(f"SHIPPROOF_API_TOKEN must be at least {MIN_TOKEN_CHARS} characters")
        if not 1 <= self.confusionThreshold <= 100:
            raise ValueError("SHIPPROOF_CONFUSION_THRESHOLD must be in [1, 100]")

    @classmethod
    def fromEnvironment(cls) -> Settings:
        try:
            threshold = int(os.environ.get("SHIPPROOF_CONFUSION_THRESHOLD", "15"))
        except ValueError as error:
            raise ValueError("SHIPPROOF_CONFUSION_THRESHOLD must be an integer") from error
        dataFile = os.environ.get("SHIPPROOF_DATA_FILE", "shipproof-data.json")
        return cls(apiToken=os.environ.get("SHIPPROOF_API_TOKEN", ""), dataFile=Path(dataFile) if dataFile else None,
                   confusionThreshold=threshold, dashboardOrigin=os.environ.get("SHIPPROOF_DASHBOARD_ORIGIN", "http://localhost:3000"))


@contextmanager
def defaultToolbox() -> Iterator[dict[str, BaseTool]]:
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
        with LinkAuditor() as auditor:
            yield {tool.name: tool for tool in buildTools(github, auditor, gmailService)}
    finally:
        github.close()


def defaultLlm() -> tuple[Optional[LlmClient], Optional[str]]:
    try:
        return GeminiClient(GeminiConfig.fromEnvironment()), None
    except LlmConfigError as error:
        return None, str(error)


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


def createApp(settings: Settings, toolboxFactory: ToolboxFactory = defaultToolbox, llmFactory: LlmFactory = defaultLlm) -> FastAPI:
    store = ProjectStore(settings.dataFile)
    bus = EventBus()
    app = FastAPI(title="ShipProof", version="0.4.0")
    app.state.store, app.state.bus = store, bus
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
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/projects", status_code=201)
    def createProject(body: ProjectBody, _: Actor = Depends(requireHuman)) -> dict[str, Any]:
        project = Project(projectId=body.projectId, repo=body.repo, proposalText=body.proposalText, links=tuple(body.links),
                          rulePages=tuple(body.rulePages), emailQuery=body.emailQuery)
        return store.createProject(project).model_dump(mode="json")

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

    @app.post("/projects/{projectId}/trigger-audit", status_code=202)
    def triggerAudit(projectId: str, _: Actor = Depends(requireHuman)) -> dict[str, str]:
        project, packetId = store.beginAudit(projectId)
        startedAt = utcNow()

        def emit(kind: str, data: dict[str, Any]) -> None:
            bus.publish(packetId, kind, data)

        def worker() -> None:
            try:
                llm, llmReason = llmFactory()
                with toolboxFactory() as tools:
                    deps = AuditDependencies(tools=tools, llm=llm, llmUnavailableReason=llmReason,
                                             confusionThreshold=settings.confusionThreshold, emit=emit)
                    packet, summary, checks = runAudit(project, packetId, deps, startedAt)
                store.completeAudit(projectId, packet, summary, checks, project.proofChecks)
                emit("done", {"approvalState": packet.approvalState.value, "confusionIndex": summary.confusionIndex,
                              "halted": summary.halted})
            except Exception as error:  # every failure ends at the human boundary with a CRITICAL flag
                failed = store.failAudit(projectId, packetId, f"{type(error).__name__}: {error}")
                emit("error", {"message": f"{type(error).__name__}: {error}"[:500]})
                emit("done", {"approvalState": failed.approvalState.value})

        emit("stage", {"stage": "queued"})
        threading.Thread(target=worker, name=f"audit-{packetId}", daemon=True).start()
        return {"packetId": packetId}

    @app.get("/packets/{packetId}")
    def getPacket(packetId: str, _: Actor = Depends(requireHuman)) -> dict[str, Any]:
        summary = store.getSummary(packetId)
        return {"packet": store.getPacket(packetId).model_dump(mode="json"),
                "summary": summary.model_dump(mode="json") if summary else None}

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
