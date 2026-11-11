import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import StructuredTool
from starlette.websockets import WebSocketDisconnect

from shipproof.agent.graph import AuditDependencies, runAudit
from shipproof.agent.project import LinkTarget, Project, computeConfusionIndex
from shipproof.agent.reconcile import reconcileDeadlines
from shipproof.agent.store import ProjectStore
from shipproof.api import Settings, createApp
from shipproof.llm import LlmResponseError
from shipproof.models import (
    ApprovalState,
    CheckStatus,
    EvidenceItem,
    EvidenceMethod,
    Severity,
    SourceKind,
    UncertaintyFlag,
    UncertaintyKind,
    utcNow,
)
from shipproof.prompts import CodeIndex, CodeIndexEntry, GroundedRule, RuleCategory
from shipproof.tools.agentTools import sourceRecordFromPage
from shipproof.tools.githubAudit import RepoAudit

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
UTC = timezone.utc
RULES_URL = "https://event.example/rules"
RULES_TEXT = "Final submissions are due 2026-11-12T18:00:00+00:00. Demo videos must be public."
PROPOSAL = "We will build a live link auditor using a headless browser for hackathon teams."


def rule(sourceId="s1", deadline=None, issued=None, ambiguous=False, supersedes=False, category=RuleCategory.DEADLINE):
    return GroundedRule(sourceId=sourceId, ruleText="Submissions due.", category=category, verbatimQuote="Submissions are due soon.",
                        issuedAt=issued, deadlineAt=deadline, dateAmbiguous=ambiguous, supersedesEarlierRule=supersedes)


D1 = datetime(2026, 11, 12, 18, tzinfo=UTC)
D2 = datetime(2026, 11, 13, 18, tzinfo=UTC)
T1 = datetime(2026, 11, 1, tzinfo=UTC)
T2 = datetime(2026, 11, 10, tzinfo=UTC)


class TestReconcile:
    def test_singleDeadline(self):
        chosen, flags = reconcileDeadlines((rule(deadline=D1),), {})
        assert chosen == D1 and flags == []

    def test_newerDatedWins(self):
        chosen, flags = reconcileDeadlines((rule("web", D1), rule("mail", D2)), {"web": T1, "mail": T2})
        assert chosen == D2 and flags[0].severity is Severity.MEDIUM

    def test_undatedConflictUnresolved(self):
        chosen, flags = reconcileDeadlines((rule("web", D1), rule("mail", D2)), {"mail": T2})
        assert chosen is None and flags[0].severity is Severity.HIGH

    def test_sameTimeConflictUnresolved(self):
        chosen, flags = reconcileDeadlines((rule("a", D1), rule("b", D2)), {"a": T1, "b": T1})
        assert chosen is None and flags[0].severity is Severity.HIGH

    def test_ruleIssuedAtOverridesSourceDate(self):
        chosen, _ = reconcileDeadlines((rule("web", D1, issued=T2), rule("mail", D2)), {"web": T1, "mail": T1})
        assert chosen == D1

    def test_missingAndAmbiguousAndRetraction(self):
        chosen, flags = reconcileDeadlines((rule(ambiguous=True), rule(category=RuleCategory.OTHER, supersedes=True)), {})
        kinds = [f.kind for f in flags]
        assert chosen is None and UncertaintyKind.UNDATED_RULE in kinds and kinds.count(UncertaintyKind.CONFLICTING_SOURCES) == 1
        assert UncertaintyKind.MISSING_SOURCE_CATEGORY in kinds


def test_confusionIndexIsStructuralAndCapped():
    def flag(severity, n):
        return UncertaintyFlag(flagId=f"f{n}", kind=UncertaintyKind.UNDATED_RULE, detail="x", severity=severity, raisedAt=utcNow())
    assert computeConfusionIndex([flag(Severity.LOW, 1), flag(Severity.HIGH, 2)]) == 7
    assert computeConfusionIndex([flag(Severity.CRITICAL, n) for n in range(20)]) == 100


# ---------------------------------------------------------------- fakes


class RoutingLlm:
    """Answers by response schema title; records calls."""

    def __init__(self, failOn=()):
        self.failOn, self.calls = set(failOn), []

    def generateJson(self, systemInstruction, userContent, responseSchema):
        title = responseSchema["title"]
        self.calls.append(title)
        if title in self.failOn:
            raise LlmResponseError("simulated overload")
        if title == "RawRuleExtraction":
            return json.dumps({"rules": [{"ruleText": "Due 12 Nov.", "category": "DEADLINE",
                                          "verbatimQuote": "Final submissions are due 2026-11-12T18:00:00+00:00.",
                                          "issuedAt": None, "deadlineAt": "2026-11-12T18:00:00+00:00", "supersedesEarlierRule": False}],
                               "suspectedInjections": []})
        if title == "RawAlignment":
            return json.dumps({"features": [{"feature": "link auditor", "proposalQuote": "a live link auditor using a headless browser",
                                             "readmeQuote": None, "codeEvidencePaths": ["audit.py"], "codeEvidenceSymbols": []}]})
        if title == "RawInterpretationCheck":
            return json.dumps({"verdict": "NO_MISREADING_FOUND", "conflicts": []})
        raise AssertionError(title)


def repoAuditPayload(public=True):
    visibility = EvidenceItem(evidenceId="repoVisibility", url="https://github.com/acme/app", method=EvidenceMethod.GITHUB_API,
                              finalStatus=200 if public else 404, authWallDetected=False,
                              status=CheckStatus.PASS if public else CheckStatus.FAIL, checkedAt=utcNow())
    return RepoAudit(owner="acme", repo="app", visibilityEvidence=visibility, isPubliclyVisible=public,
                     readmeStatus=CheckStatus.PASS, readmeText="# App", licenseStatus=CheckStatus.PASS, licenseSpdx="MIT",
                     defaultBranch="main", commitMessages=("init",),
                     codeIndex=CodeIndex(entries=(CodeIndexEntry(path="audit.py", symbols=("auditLink",)),))).model_dump(mode="json")


def fakeTools(public=True, linkAuthWall=False, pageReachable=True, repoRaises=False):
    def auditRepository(repo: str):
        if repoRaises:
            raise RuntimeError("GitHub down")
        return repoAuditPayload(public)

    def auditLink(url: str, evidenceId: str):
        return EvidenceItem(evidenceId=evidenceId, url=url, method=EvidenceMethod.HEADLESS_BROWSER,
                            finalStatus=200, authWallDetected=linkAuthWall,
                            status=CheckStatus.FAIL if linkAuthWall else CheckStatus.PASS, checkedAt=utcNow(),
                            detail="Auth wall: url:/signin" if linkAuthWall else "ok").model_dump(mode="json")

    def readRulesPage(url: str):
        if not pageReachable:
            from shipproof.tools.agentTools import unreachableSource
            return {"source": unreachableSource(url, SourceKind.EVENT_PAGE, "status 404").model_dump(mode="json"), "text": None}
        return {"source": sourceRecordFromPage(url, RULES_TEXT).model_dump(mode="json"), "text": RULES_TEXT}

    return {t.name: t for t in (StructuredTool.from_function(f, name=f.__name__, description=f.__name__)
                                for f in (auditRepository, auditLink, readRulesPage))}


def makeProject(**overrides):
    data = dict(projectId="demo", repo="acme/app", proposalText=PROPOSAL,
                links=(LinkTarget(label="Demo video", url="https://video.example/v1"),), rulePages=(RULES_URL,))
    data.update(overrides)
    return Project(**data)


def deps(tools=None, llm="default", threshold=40, events=None):
    return AuditDependencies(tools=tools or fakeTools(), llm=RoutingLlm() if llm == "default" else llm,
                             llmUnavailableReason="GEMINI_API_KEY missing" if llm is None else None,
                             confusionThreshold=threshold, emit=lambda k, d: events.append(k) if events is not None else None)


class TestGraph:
    def test_happyPathEndsPendingWithEvidence(self):
        events = []
        packet, summary, _ = runAudit(makeProject(), "pkt-1", deps(events=events), utcNow())
        assert packet.approvalState is ApprovalState.PENDING_HUMAN_APPROVAL
        assert summary.effectiveDeadline == D1 and not summary.halted
        assert {e.evidenceId for e in packet.evidence} == {"repoVisibility", "repoReadme", "repoLicense", "link1"}
        assert packet.reports[0].alignmentScore == 100
        assert [t.tool for t in summary.plan] == ["auditRepository", "auditLink", "assessAlignment"]
        assert ["stage", "plan", "confusion"] <= list(dict.fromkeys(events))
        kinds = {f.kind for f in packet.uncertaintyFlags}
        assert kinds == {UncertaintyKind.MISSING_SOURCE_CATEGORY}  # inbox not checked
        assert not packet.ledger.humanConfirmedComplete

    def test_authWallAndPrivateRepoRaiseCriticalFlags(self):
        packet, _, _ = runAudit(makeProject(), "pkt-1", deps(fakeTools(public=False, linkAuthWall=True)), utcNow())
        critical = [f for f in packet.uncertaintyFlags if f.severity is Severity.CRITICAL]
        assert len(critical) == 2 and all(f.kind is UncertaintyKind.AUTH_REDIRECT for f in critical)

    def test_haltsAboveThresholdBeforeAuditing(self):
        events = []
        packet, summary, _ = runAudit(makeProject(), "pkt-1", deps(fakeTools(pageReachable=False), threshold=5, events=events), utcNow())
        assert summary.halted and summary.plan == () and packet.evidence == ()
        assert any(f.kind is UncertaintyKind.CONFUSION_THRESHOLD_EXCEEDED for f in packet.uncertaintyFlags)
        assert "plan" not in events

    def test_definiteFailuresAreEvidenceNotFlags(self):
        def thinRepo(repo: str):
            payload = repoAuditPayload()
            payload.update(readmeStatus="FAIL", licenseStatus="FAIL", licenseSpdx=None)
            return payload
        tools = fakeTools()
        tools["auditRepository"] = StructuredTool.from_function(thinRepo, name="auditRepository", description="x")
        packet, _, _ = runAudit(makeProject(), "pkt-1", deps(tools), utcNow())
        statuses = {e.evidenceId: e.status for e in packet.evidence}
        assert statuses["repoReadme"] is CheckStatus.FAIL and statuses["repoLicense"] is CheckStatus.FAIL
        assert all("README" not in f.detail and "license" not in f.detail for f in packet.uncertaintyFlags)

    def test_thresholdRecheckedAfterAudit(self):
        packet, summary, _ = runAudit(makeProject(), "pkt-1", deps(fakeTools(linkAuthWall=True), threshold=12), utcNow())
        assert not summary.halted and summary.confusionIndex > 12
        assert packet.uncertaintyFlags[-1].kind is UncertaintyKind.CONFUSION_THRESHOLD_EXCEEDED

    def test_llmUnavailableDegradesToFlags(self):
        packet, _, _ = runAudit(makeProject(), "pkt-1", deps(llm=None), utcNow())
        assert packet.reports == () and any(f.kind is UncertaintyKind.ANALYSIS_FAILED for f in packet.uncertaintyFlags)
        assert any(e.evidenceId == "link1" for e in packet.evidence), "deterministic checks still run"

    def test_llmFailureFlaggedNotRaised(self):
        packet, _, _ = runAudit(makeProject(), "pkt-1", deps(llm=RoutingLlm(failOn={"RawAlignment"})), utcNow())
        assert any("Alignment check failed" in f.detail for f in packet.uncertaintyFlags)

    def test_repoToolErrorBecomesUnreachableSource(self):
        packet, _, _ = runAudit(makeProject(), "pkt-1", deps(fakeTools(repoRaises=True)), utcNow())
        assert any(s.kind is SourceKind.REPOSITORY and s.status.value == "UNREACHABLE" for s in packet.ledger.sources)


# ---------------------------------------------------------------- API


@pytest.fixture
def api(tmp_path):
    tools = {"value": fakeTools()}

    @contextmanager
    def toolbox():
        yield tools["value"]

    settings = Settings(apiToken=TOKEN, dataFile=tmp_path / "data.json", confusionThreshold=40)
    app = createApp(settings, toolboxFactory=toolbox, llmFactory=lambda: (RoutingLlm(), None))
    with TestClient(app) as client:
        client.tools = tools
        client.dataFile = settings.dataFile
        yield client


def waitForPending(client, packetId, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/packets/{packetId}", headers=AUTH).json()
        if body["packet"]["approvalState"] != "AUDITING":
            return body
        time.sleep(0.05)
    raise AssertionError("audit did not finish")


def createDemo(client, links=None):
    body = {"projectId": "demo", "repo": "acme/app", "proposalText": PROPOSAL, "rulePages": [RULES_URL],
            "links": links if links is not None else [{"label": "Demo video", "url": "https://video.example/v1"}]}
    assert client.post("/projects", json=body, headers=AUTH).status_code == 201


class TestApi:
    def test_authRequired(self, api):
        assert api.get("/projects/demo").status_code == 401
        assert api.get("/projects/demo", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert api.get("/health").status_code == 200

    def test_settingsRejectWeakToken(self):
        with pytest.raises(ValueError):
            Settings(apiToken="short")

    def test_invalidProjectRejected(self, api):
        response = api.post("/projects", json={"projectId": "bad id!", "repo": "a/b", "proposalText": "x"}, headers=AUTH)
        assert response.status_code == 422

    def test_fullFlowApprovalBoundary(self, api):
        createDemo(api)
        ref = {"kind": "URL", "value": RULES_URL}
        assert api.post("/projects/demo/clauses", json={"clauseId": "c1", "text": "Demo video must be public.", "sourceRef": ref}, headers=AUTH).status_code == 201
        assert api.post("/projects/demo/proof-checks", json={"checkId": "p1", "clauseId": "c1", "interpretation": "Public YouTube link."}, headers=AUTH).status_code == 201

        packetId = api.post("/projects/demo/trigger-audit", headers=AUTH).json()["packetId"]
        body = waitForPending(api, packetId)
        assert body["packet"]["approvalState"] == "PENDING_HUMAN_APPROVAL" and body["summary"]["effectiveDeadline"]

        project = api.get("/projects/demo", headers=AUTH).json()
        assert project["proofChecks"][0]["status"] == "PASS", "audit writes proof status back to the project"
        assert api.post("/projects/demo/proof-checks/p1/promote", headers=AUTH).status_code == 200

        assert api.post(f"/packets/{packetId}/approve", headers=AUTH).status_code == 422, "ledger not confirmed"
        assert api.post(f"/packets/{packetId}/coverage/confirm", headers=AUTH).json()["ledger"]["humanConfirmedComplete"]
        approved = api.post(f"/packets/{packetId}/approve", headers=AUTH)
        assert approved.status_code == 200 and approved.json()["decidedBy"]["kind"] == "HUMAN"
        assert api.post(f"/packets/{packetId}/reject", headers=AUTH).status_code == 409, "terminal state"

    def test_criticalFlagBlocksApprovalButAllowsReject(self, api):
        api.tools["value"] = fakeTools(linkAuthWall=True)
        createDemo(api)
        packetId = api.post("/projects/demo/trigger-audit", headers=AUTH).json()["packetId"]
        waitForPending(api, packetId)
        api.post(f"/packets/{packetId}/coverage/confirm", headers=AUTH)
        assert api.post(f"/packets/{packetId}/approve", headers=AUTH).status_code == 422
        assert api.post(f"/packets/{packetId}/reject", headers=AUTH).json()["approvalState"] == "REJECTED"

    def test_amendmentMarksStaleViaApi(self, api):
        createDemo(api)
        ref = {"kind": "URL", "value": RULES_URL}
        api.post("/projects/demo/clauses", json={"clauseId": "c1", "text": "Video public.", "sourceRef": ref}, headers=AUTH)
        api.post("/projects/demo/proof-checks", json={"checkId": "p1", "clauseId": "c1", "interpretation": "Public link."}, headers=AUTH)
        noRef = api.post("/projects/demo/amendments", json={"amendmentId": "a1", "clauseId": "c1", "newText": "Under 3 min."}, headers=AUTH)
        assert noRef.status_code == 422
        amended = api.post("/projects/demo/amendments", json={"amendmentId": "a1", "clauseId": "c1", "newText": "Public, under 3 min.",
                                                              "sourceRef": {"kind": "EMAIL_MESSAGE_ID", "value": "<x@event.dev>"}}, headers=AUTH)
        assert amended.json()["proofChecks"][0]["status"] == "STALE"
        assert api.post("/projects/demo/proof-checks/p1/promote", headers=AUTH).status_code == 409

    def test_toolboxFailureEndsAtHumanBoundary(self, api):
        createDemo(api)
        api.tools["value"] = None  # any crash inside the worker, e.g. Chromium missing
        packetId = api.post("/projects/demo/trigger-audit", headers=AUTH).json()["packetId"]
        body = waitForPending(api, packetId)
        flags = body["packet"]["uncertaintyFlags"]
        assert body["packet"]["approvalState"] == "PENDING_HUMAN_APPROVAL" and flags[-1]["severity"] == "CRITICAL"

    def test_concurrentAuditRejected(self, api):
        createDemo(api)
        store: ProjectStore = api.app.state.store
        store.beginAudit("demo")
        assert api.post("/projects/demo/trigger-audit", headers=AUTH).status_code == 409

    def test_persistenceAcrossRestart(self, api, tmp_path):
        createDemo(api)
        reloaded = ProjectStore(api.dataFile)
        assert reloaded.getProject("demo").repo == "acme/app"

    def test_interruptedAuditRecoveredOnLoad(self, tmp_path):
        store = ProjectStore(tmp_path / "d.json")
        store.createProject(makeProject())
        _, packetId = store.beginAudit("demo")
        packet = ProjectStore(tmp_path / "d.json").getPacket(packetId)
        assert packet.approvalState is ApprovalState.PENDING_HUMAN_APPROVAL
        assert packet.uncertaintyFlags[-1].kind is UncertaintyKind.ANALYSIS_FAILED

    def test_websocketRequiresTokenThenStreams(self, api):
        createDemo(api)
        packetId = api.post("/projects/demo/trigger-audit", headers=AUTH).json()["packetId"]
        waitForPending(api, packetId)
        with api.websocket_connect(f"/ws/packets/{packetId}") as socket:
            socket.send_json({"token": "wrong"})
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 4401
        with api.websocket_connect(f"/ws/packets/{packetId}") as socket:
            socket.send_json({"token": TOKEN})
            kinds = []
            while "done" not in kinds:
                kinds.append(socket.receive_json()["kind"])
            assert kinds[0] == "stage" and "plan" in kinds
