import json
import logging
import time
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import StructuredTool

from shipproof.agent.graph import AuditDependencies, newCheckpointer, runAudit
from shipproof.agent.investigate import InvestigationAction, planInvestigation
from shipproof.agent.memory import ProjectMemory, StepOrigin, computeDiff, flagKey
from shipproof.agent.project import LinkTarget, Project
from shipproof.api import Settings, createApp
from shipproof.demo import DEMO_REPO, DemoSiteServer, _matches, demoEmails, demoProject, demoRepoAudit
from shipproof.llm import BudgetedLlm, GeminiClient, GeminiConfig, LlmConfigError, LlmResponseError, LlmRuntime, ReplayLlm, promptKey, selectModelChain
from shipproof.models import ApprovalState, CheckStatus, EvidenceItem, EvidenceMethod, Severity, UncertaintyKind, utcNow
from shipproof.tools.agentTools import sourceRecordFromPage
from tests.test_agent import AUTH, TOKEN, fakeTools, repoAuditPayload

RULES_URL = "https://event.example/rules"
UPDATES_URL = "https://event.example/updates"
RULES_TEXT = "Final submissions are due 2026-11-12T18:00:00+00:00. Read the updates page."
UPDATES_TEXT = "Rules update: the deadline is extended to 2026-11-13T18:00:00+00:00 for all teams."
PROPOSAL = "We will build a live link auditor using a headless browser for teams."


# ---------------------------------------------------------------- 1 + 2: model selection, runtime, budget


class FakeSdk:
    def __init__(self, names, answer='{"ok": true}', failModels=()):
        self.models = self
        self.names, self.answer, self.failModels, self.calls = names, answer, set(failModels), []

    def list(self):
        return [SimpleNamespace(name=f"models/{n}", supported_actions=["generateContent"]) for n in self.names]

    def generate_content(self, model, contents, config):
        from google.genai import errors
        self.calls.append(model)
        if model in self.failModels:
            raise errors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
        return SimpleNamespace(text=self.answer, candidates=[SimpleNamespace(finish_reason="FinishReason.STOP")], prompt_feedback=None)


class TestModelSetup:
    def test_selectsNewestStableFlashThenFallbacks(self):
        chain = selectModelChain(["gemini-2.5-flash", "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.8-flash-image",
                                  "gemini-3-pro", "gemini-3.9-flash-preview-11-2026", "text-embedding-004"])
        assert chain[0] == "gemini-3.8-flash" and "gemini-3.8-flash-image" not in chain and "gemini-3-pro" not in chain

    def test_explicitModelMustExist(self):
        with pytest.raises(LlmConfigError, match="Available flash models: gemini-3.5-flash"):
            selectModelChain(["gemini-3.5-flash"], "gemini-9-flash")
        assert selectModelChain(["gemini-3.5-flash", "gemini-3.8-flash"], "gemini-3.5-flash")[0] == "gemini-3.5-flash"

    def test_noFlashModels(self):
        with pytest.raises(LlmConfigError):
            selectModelChain(["gemini-3-pro"])

    def test_runtimeResolvesAndProbes(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        sdk = FakeSdk(["gemini-3.5-flash", "gemini-3.8-flash"])
        runtime = LlmRuntime()
        runtime.resolve(lambda config: GeminiClient(config, sdkClient=sdk))
        assert runtime.snapshot()["status"] == "ready" and runtime.model == "gemini-3.8-flash" and sdk.calls == ["gemini-3.8-flash"]

    def test_runtimeReportsWhyUnavailable(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setenv("GEMINI_MODEL", "gemini-9-flash")
        runtime = LlmRuntime()
        runtime.resolve(lambda config: GeminiClient(config, sdkClient=FakeSdk(["gemini-3.5-flash"])))
        assert runtime.status == "unavailable" and "gemini-3.5-flash" in runtime.reason
        monkeypatch.delenv("GEMINI_API_KEY")
        runtime.resolve()
        assert runtime.status == "unavailable" and "GEMINI_API_KEY" in runtime.reason

    def test_defaultsAreShort(self):
        config = GeminiConfig(apiKey="k")
        assert config.timeoutSeconds == 20 and config.maxAttempts == 2

    def test_budgetStopsCallsAndFallbacks(self):
        sdk = FakeSdk(["a"], failModels={"gemini-3.8-flash"})
        client = GeminiClient(GeminiConfig(apiKey="k"), sdkClient=sdk)
        with pytest.raises(LlmResponseError, match="budget"):
            BudgetedLlm(client, time.monotonic() - 1).generateJson("s", "u", {})
        assert sdk.calls == []
        with pytest.raises(LlmResponseError, match="skipped, audit time budget"):
            client.withDeadline(time.monotonic() - 1).generateJson("s", "u", {})


# ---------------------------------------------------------------- 3: replay


class CountingLlm:
    def __init__(self):
        self.calls = 0

    def generateJson(self, system, user, schema):
        self.calls += 1
        return json.dumps({"n": self.calls})


class TestReplay:
    def test_recordThenReplayIgnoresNonce(self, tmp_path):
        cache = tmp_path / "cache.json"
        recorder = ReplayLlm(cache, "record", CountingLlm())
        first = recorder.generateJson('nonce="' + "a" * 32 + '"', 'x nonce="' + "a" * 32 + '"', {"title": "T"})
        replay = ReplayLlm(cache, "replay")
        assert replay.generateJson('nonce="' + "b" * 32 + '"', 'x nonce="' + "b" * 32 + '"', {"title": "T"}) == first
        with pytest.raises(LlmResponseError, match="re-record"):
            replay.generateJson("other", "prompt", {"title": "T"})

    def test_keyChangesWithContent(self):
        assert promptKey("s", "u1", {}) != promptKey("s", "u2", {})

    def test_malformedCacheRejected(self, tmp_path):
        (tmp_path / "c.json").write_text("[1,2]")
        with pytest.raises(LlmConfigError):
            ReplayLlm(tmp_path / "c.json", "replay")
        with pytest.raises(LlmConfigError):
            ReplayLlm(tmp_path / "none.json", "record")


# ---------------------------------------------------------------- 4: planner guardrails


class PlannerLlm:
    def __init__(self, steps):
        self.steps = steps

    def generateJson(self, system, user, schema):
        return json.dumps({"steps": self.steps})


class TestPlanner:
    def test_onlyGroundedTargetsAccepted(self):
        llm = PlannerLlm([{"action": "READ_PAGE", "target": UPDATES_URL, "reason": "May hold a newer deadline."},
                          {"action": "CHECK_LINK", "target": "https://evil.example/exfiltrate", "reason": "x"},
                          {"action": "SEARCH_INBOX", "target": "deadline", "reason": "Find organiser emails."}])
        visited = {RULES_URL}
        steps, rejected, _ = planInvestigation(llm, "findings", [UPDATES_URL, RULES_URL], False, visited)
        assert [(s.action, s.target) for s in steps] == [(InvestigationAction.READ_PAGE, UPDATES_URL)]
        assert rejected == 2 and UPDATES_URL in visited

    def test_noRepeatVisits(self):
        llm = PlannerLlm([{"action": "READ_PAGE", "target": UPDATES_URL, "reason": "again"}])
        assert planInvestigation(llm, "f", [UPDATES_URL], False, {UPDATES_URL})[0] == []

    def test_stopEndsPlan(self):
        llm = PlannerLlm([{"action": "STOP", "target": "", "reason": "done"}, {"action": "READ_PAGE", "target": UPDATES_URL, "reason": "x"}])
        assert planInvestigation(llm, "f", [UPDATES_URL], False, set())[0] == []


# ---------------------------------------------------------------- 5 + 6 + 7: scenario through the graph


class ScenarioLlm:
    """Rules come from whatever source text is in the prompt; the planner reads the updates page once."""

    def __init__(self, investigate=True):
        self.investigate = investigate
        self.titles = []

    def generateJson(self, system, user, schema):
        title = schema["title"]
        self.titles.append(title)
        if title == "RawRuleExtraction":
            rules = []
            if "due 2026-11-12T18:00:00+00:00" in user:
                rules.append({"ruleText": "Due 12 Nov.", "category": "DEADLINE", "verbatimQuote": "Final submissions are due 2026-11-12T18:00:00+00:00.",
                              "issuedAt": None, "deadlineAt": "2026-11-12T18:00:00+00:00", "supersedesEarlierRule": False})
            if "extended to 2026-11-13T18:00:00+00:00" in user:
                rules.append({"ruleText": "Extended to 13 Nov.", "category": "DEADLINE",
                              "verbatimQuote": "the deadline is extended to 2026-11-13T18:00:00+00:00 for all teams",
                              "issuedAt": None, "deadlineAt": "2026-11-13T18:00:00+00:00", "supersedesEarlierRule": True})
            return json.dumps({"rules": rules, "suspectedInjections": []})
        if title == "RawInvestigation":
            if self.investigate and UPDATES_URL in user:
                return json.dumps({"steps": [{"action": "READ_PAGE", "target": UPDATES_URL, "reason": "The rules page links to updates."}]})
            return json.dumps({"steps": [{"action": "STOP", "target": "", "reason": "Nothing left."}]})
        if title == "RawAlignment":
            return json.dumps({"features": [{"feature": "link auditor", "proposalQuote": "a live link auditor using a headless browser",
                                             "readmeQuote": None, "codeEvidencePaths": ["audit.py"], "codeEvidenceSymbols": []}]})
        if title == "RawInterpretationCheck":
            return json.dumps({"verdict": "NO_MISREADING_FOUND", "conflicts": []})
        raise AssertionError(title)


def scenarioTools(videoPublicOnRetry=False, alternative=None):
    tools = fakeTools()
    attempts = {"video": 0}

    def readRulesPage(url: str):
        text = {RULES_URL: RULES_TEXT, UPDATES_URL: UPDATES_TEXT}.get(url)
        links = [UPDATES_URL, *([alternative] if alternative else [])] if url == RULES_URL else []
        return {"source": sourceRecordFromPage(url, text).model_dump(mode="json"), "text": text, "links": links}

    def auditLink(url: str, evidenceId: str):
        private = url.endswith("/v1") and not (videoPublicOnRetry and attempts["video"] >= 1)
        if url.endswith("/v1"):
            attempts["video"] += 1
        return EvidenceItem(evidenceId=evidenceId, url=url, method=EvidenceMethod.HEADLESS_BROWSER, finalStatus=200,
                            authWallDetected=private, status=CheckStatus.FAIL if private else CheckStatus.PASS, checkedAt=utcNow(),
                            detail="Auth wall: text:this video is private" if private else "ok").model_dump(mode="json")

    tools["readRulesPage"] = StructuredTool.from_function(readRulesPage, name="readRulesPage", description="x")
    tools["auditLink"] = StructuredTool.from_function(auditLink, name="auditLink", description="x")
    return tools


def scenarioProject(memory=ProjectMemory()):
    return Project(projectId="demo", repo="acme/app", proposalText=PROPOSAL, rulePages=(RULES_URL,),
                   links=(LinkTarget(label="Demo video", url="https://video.example/v1"),), memory=memory)


def scenarioDeps(tools=None, llm=None, checkpointer=None, events=None, previous=None, budget=90):
    return AuditDependencies(tools=tools or scenarioTools(), llm=llm or ScenarioLlm(), llmUnavailableReason=None,
                             confusionThreshold=40, emit=lambda k, d: events.append((k, d)) if events is not None else None,
                             budgetSeconds=budget, deadline=time.monotonic() + budget, previousPacket=previous,
                             checkpointer=checkpointer or newCheckpointer(), llmModel="fake")


class TestScenario:
    def test_agentChoosesStepThenAsksAndResumes(self, caplog):
        checkpointer = newCheckpointer()
        events = []
        project = scenarioProject()
        with caplog.at_level(logging.WARNING):
            paused = runAudit(project, "pkt-1", scenarioDeps(checkpointer=checkpointer, events=events), utcNow())
        assert paused.paused and paused.packet is None
        agentSteps = [s for s in paused.summary.agentSteps if s.origin is StepOrigin.AGENT]
        assert agentSteps[0].tool == "readRulesPage" and agentSteps[0].target == UPDATES_URL and "updates" in agentSteps[0].reason
        values = [o.value for o in paused.question.options]
        assert values[:2] == ["2026-11-12T18:00:00+00:00", "2026-11-13T18:00:00+00:00"] and values[-1] == "unsure"

        owner = {"actorId": "owner", "kind": "HUMAN", "role": "OWNER"}
        done = runAudit(project, "pkt-1", scenarioDeps(checkpointer=checkpointer, events=events), paused.summary.startedAt,
                        resume={"value": "2026-11-13T18:00:00+00:00", "actor": owner})
        assert not done.paused and done.summary.effectiveDeadline.isoformat() == "2026-11-13T18:00:00+00:00"
        assert not any(f.kind is UncertaintyKind.CONFLICTING_SOURCES and f.severity is Severity.HIGH for f in done.packet.uncertaintyFlags)
        assert done.decisions[0].answer.value == "2026-11-13T18:00:00+00:00"
        assert done.summary.questions[0].answer.value == "2026-11-13T18:00:00+00:00"
        assert not [r for r in caplog.records if "unregistered type" in r.getMessage()], "checkpoint types must be allowlisted"

    def test_rememberedAnswerIsReusedWithoutAsking(self):
        checkpointer = newCheckpointer()
        first = runAudit(scenarioProject(), "pkt-1", scenarioDeps(checkpointer=checkpointer), utcNow())
        owner = {"actorId": "owner", "kind": "HUMAN", "role": "OWNER"}
        done = runAudit(scenarioProject(), "pkt-1", scenarioDeps(checkpointer=checkpointer), utcNow(), resume={"value": "2026-11-13T18:00:00+00:00", "actor": owner})
        memory = ProjectMemory().withDecision(done.decisions[0])
        second = runAudit(scenarioProject(memory), "pkt-2", scenarioDeps(), utcNow())
        assert not second.paused and second.summary.effectiveDeadline.isoformat() == "2026-11-13T18:00:00+00:00"
        assert any(s.origin is StepOrigin.MEMORY and "reused answer" in (s.outcome or "") for s in second.summary.agentSteps)
        assert first.question.questionKey == second.summary.questions[0].questionKey

    def test_unsureKeepsConflictFlagged(self):
        checkpointer = newCheckpointer()
        runAudit(scenarioProject(), "pkt-1", scenarioDeps(checkpointer=checkpointer), utcNow())
        done = runAudit(scenarioProject(), "pkt-1", scenarioDeps(checkpointer=checkpointer), utcNow(),
                        resume={"value": "unsure", "actor": {"actorId": "o", "kind": "HUMAN", "role": "OWNER"}})
        assert done.summary.effectiveDeadline is None
        assert any(f.followUp and "unsure" in f.followUp for f in done.packet.uncertaintyFlags)

    def test_agentCannotAnswerItsOwnQuestion(self):
        checkpointer = newCheckpointer()
        runAudit(scenarioProject(), "pkt-1", scenarioDeps(checkpointer=checkpointer), utcNow())
        with pytest.raises(ValueError):
            runAudit(scenarioProject(), "pkt-1", scenarioDeps(checkpointer=checkpointer), utcNow(),
                     resume={"value": "unsure", "actor": {"actorId": "bot", "kind": "AGENT", "role": None}})

    def test_reflectRetriesAndFindsAlternative(self):
        alternative = "https://video.example/v2"
        done = runAudit(scenarioProject(), "pkt-1", scenarioDeps(tools=scenarioTools(alternative=alternative), llm=ScenarioLlm(investigate=False)), utcNow())
        flag = next(f for f in done.packet.uncertaintyFlags if "link1" in f.sourceIds)
        assert flag.severity is Severity.CRITICAL and "Re-checked: still failing" in flag.followUp and alternative in flag.followUp
        assert any(s.origin is StepOrigin.FOLLOW_UP for s in done.summary.agentSteps)

    def test_transientFailureClearedOnRetry(self):
        done = runAudit(scenarioProject(), "pkt-1", scenarioDeps(tools=scenarioTools(videoPublicOnRetry=True), llm=ScenarioLlm(investigate=False)), utcNow())
        assert not any("link1" in f.sourceIds for f in done.packet.uncertaintyFlags)
        assert next(e for e in done.packet.evidence if e.evidenceId == "link1").status is CheckStatus.PASS

    def test_budgetExhaustedStopsInvestigation(self):
        deps = scenarioDeps(llm=ScenarioLlm(), budget=90)
        deps = AuditDependencies(**{**deps.__dict__, "deadline": time.monotonic() - 1})
        done = runAudit(scenarioProject(), "pkt-1", deps, utcNow())
        assert done.summary.budgetExhausted

    def test_diffAgainstPreviousAudit(self):
        first = runAudit(scenarioProject(), "pkt-1", scenarioDeps(tools=scenarioTools(alternative="https://video.example/v2"), llm=ScenarioLlm(investigate=False)), utcNow())
        second = runAudit(scenarioProject(), "pkt-2", scenarioDeps(tools=scenarioTools(videoPublicOnRetry=True), llm=ScenarioLlm(investigate=False),
                                                                    previous=first.packet), utcNow())
        assert second.summary.diff.previousPacketId == "pkt-1"
        assert any("link1" in label for label in second.summary.diff.fixedEvidence)
        assert any("login wall" in detail for detail in second.summary.diff.resolvedFlags)

    def test_flagKeyStableAcrossTimestamps(self):
        a = flagKey(UncertaintyKind.UNDATED_RULE, "Deadline at 2026-11-12T18:00:00+00:00 is undated")
        b = flagKey(UncertaintyKind.UNDATED_RULE, "Deadline at 2026-11-13T09:00:00Z is undated")
        assert a == b and computeDiff(None, [], []) is None


# ---------------------------------------------------------------- API: answers, accept risk, health


@pytest.fixture
def agenticApi(tmp_path):
    @contextmanager
    def toolbox():
        yield scenarioTools()

    app = createApp(Settings(apiToken=TOKEN, dataFile=tmp_path / "d.json", confusionThreshold=40),
                    toolboxFactory=toolbox, llmFactory=lambda: (ScenarioLlm(), None))
    with TestClient(app) as client:
        client.post("/projects", json={"projectId": "demo", "repo": "acme/app", "proposalText": PROPOSAL, "rulePages": [RULES_URL],
                                       "links": [{"label": "Demo video", "url": "https://video.example/v1"}]}, headers=AUTH)
        yield client


def waitForState(client, packetId, states, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/packets/{packetId}", headers=AUTH).json()
        if body["packet"]["approvalState"] in states:
            return body
        time.sleep(0.05)
    raise AssertionError(f"packet never reached {states}")


class TestAgenticApi:
    def test_questionAnswerResumeAndMemory(self, agenticApi):
        packetId = agenticApi.post("/projects/demo/trigger-audit", headers=AUTH).json()["packetId"]
        paused = waitForState(agenticApi, packetId, {"AWAITING_INPUT"})
        question = paused["summary"]["questions"][-1]
        assert agenticApi.post("/projects/demo/trigger-audit", headers=AUTH).status_code == 409, "one audit at a time, also while paused"
        assert agenticApi.post(f"/packets/{packetId}/answers", json={"questionId": "q9", "value": "unsure"}, headers=AUTH).status_code == 409
        assert agenticApi.post(f"/packets/{packetId}/answers", json={"questionId": question["questionId"], "value": "2030-01-01"}, headers=AUTH).status_code == 409
        assert agenticApi.post(f"/packets/{packetId}/answers", json={"questionId": question["questionId"], "value": "unsure"}).status_code == 401
        resumed = agenticApi.post(f"/packets/{packetId}/answers", json={"questionId": question["questionId"], "value": "2026-11-13T18:00:00+00:00"}, headers=AUTH)
        assert resumed.status_code == 202
        done = waitForState(agenticApi, packetId, {"PENDING_HUMAN_APPROVAL"})
        assert done["summary"]["effectiveDeadline"] == "2026-11-13T18:00:00Z"
        memory = agenticApi.get("/projects/demo", headers=AUTH).json()["memory"]
        assert memory["decisions"][0]["answer"]["value"] == "2026-11-13T18:00:00+00:00"

        second = agenticApi.post("/projects/demo/trigger-audit", headers=AUTH).json()["packetId"]
        body = waitForState(agenticApi, second, {"PENDING_HUMAN_APPROVAL", "AWAITING_INPUT"})
        assert body["packet"]["approvalState"] == "PENDING_HUMAN_APPROVAL", "remembered answer: no second question"
        assert body["summary"]["diff"]["previousPacketId"] == packetId

    def test_acceptedRiskUnblocksAndIsRemembered(self, agenticApi):
        packetId = agenticApi.post("/projects/demo/trigger-audit", headers=AUTH).json()["packetId"]
        question = waitForState(agenticApi, packetId, {"AWAITING_INPUT"})["summary"]["questions"][-1]
        agenticApi.post(f"/packets/{packetId}/answers", json={"questionId": question["questionId"], "value": "2026-11-13T18:00:00+00:00"}, headers=AUTH)
        packet = waitForState(agenticApi, packetId, {"PENDING_HUMAN_APPROVAL"})["packet"]
        agenticApi.post(f"/packets/{packetId}/coverage/confirm", headers=AUTH)
        critical = [f for f in packet["uncertaintyFlags"] if f["severity"] == "CRITICAL"]
        assert critical and agenticApi.post(f"/packets/{packetId}/approve", headers=AUTH).status_code == 422
        assert agenticApi.post(f"/packets/{packetId}/flags/{critical[0]['flagId']}/accept", json={"reason": ""}, headers=AUTH).status_code == 422
        for flag in critical:
            accepted = agenticApi.post(f"/packets/{packetId}/flags/{flag['flagId']}/accept",
                                       json={"reason": "Video goes public at submission time."}, headers=AUTH)
            assert accepted.status_code == 200
        assert agenticApi.post(f"/packets/{packetId}/approve", headers=AUTH).status_code == 200
        second = agenticApi.post("/projects/demo/trigger-audit", headers=AUTH).json()["packetId"]
        flags = waitForState(agenticApi, second, {"PENDING_HUMAN_APPROVAL"})["packet"]["uncertaintyFlags"]
        remembered = [f for f in flags if f["severity"] == "CRITICAL"]
        assert remembered and all(f["acceptedReason"].startswith("Accepted earlier") for f in remembered)

    def test_healthReportsModel(self, agenticApi):
        health = agenticApi.get("/health").json()
        assert health["llm"] == {"status": "injected"} and health["budgetSeconds"] == 90 and health["demo"] is False

    def test_settingsValidation(self):
        with pytest.raises(ValueError):
            Settings(apiToken=TOKEN, budgetSeconds=5)
        with pytest.raises(ValueError):
            Settings(apiToken=TOKEN, llmMode="guess")


# ---------------------------------------------------------------- 3: demo fixtures


class TestDemo:
    def test_fixtureRepoHasRoutesAndPlantedProblems(self):
        audit = demoRepoAudit("http://127.0.0.1:8799")
        assert audit["licenseStatus"] == "FAIL" and audit["readmeStatus"] == "PASS"
        symbols = {s for entry in audit["codeIndex"]["entries"] for s in entry["symbols"]}
        assert {"POST /audit", "POST /deadlines/reconcile", "auditLink", "reconcileDeadlines"} <= symbols
        assert "http://127.0.0.1:8799/slides-old.html" in audit["readmeText"]

    def test_inboxMatcher(self):
        message = demoEmails()[0]
        assert _matches(message, "from:organisers@demo.event newer_than:30d")
        assert not _matches(message, "from:organisers@demo.event deadline")

    def test_demoProjectIsValid(self):
        project = demoProject("http://127.0.0.1:8799")
        assert project.repo == DEMO_REPO and project.charter.currentClause("c1").version == 1 and project.proofChecks[0].status is CheckStatus.UNVERIFIED

    def test_siteServesPagesAnd404(self):
        server = DemoSiteServer(18799)
        try:
            with urllib.request.urlopen(f"{server.base}/rules.html") as response:
                body = response.read().decode()
            assert "2026-11-14T18:00:00+00:00" in body and f"{server.base}/updates.html" in body
            with pytest.raises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(f"{server.base}/slides-old.html")
            assert missing.value.code == 404
            missing.value.close()
        finally:
            server.close()
