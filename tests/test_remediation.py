import base64
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import StructuredTool
from pydantic import ValidationError

from shipproof.agent.graph import runAudit
from shipproof.api import Settings, createApp
from shipproof.models import CheckStatus, EvidenceItem, EvidenceMethod, utcNow
from shipproof.prompts import CodeIndex, CodeIndexEntry, FeatureAssessment
from shipproof.remediation import (
    FileChange,
    RemediationKind,
    draftReadme,
    extractReadmeLinks,
    featureGuidance,
    fillLicenseTemplate,
    readmeLinksRemediation,
    removeLinks,
)
from shipproof.tools.githubAudit import GitHubError
from shipproof.tools.githubWrite import GitHubWriter
from tests.test_agent import AUTH, TOKEN, RoutingLlm, deps, fakeTools, makeProject, repoAuditPayload, waitForPending

INDEX = CodeIndex(entries=(
    CodeIndexEntry(path="requirements.txt"),
    CodeIndexEntry(path="api/server.py", symbols=("triggerAudit", "POST /trigger-audit")),
    CodeIndexEntry(path="web/package.json"),
))
DONE = FeatureAssessment(feature="audit endpoint", proposalQuote="an audit endpoint for teams", codeEvidencePaths=("api/server.py",), codeEvidenceSymbols=())
MISSING = FeatureAssessment(feature="slack alerts", proposalQuote="alerts sent to slack channels", codeEvidencePaths=(), codeEvidenceSymbols=())


class TestDrafting:
    def test_readmeStatesOnlyVerifiedFacts(self):
        readme = draftReadme("app", INDEX, (DONE, MISSING))
        features = readme.split("## Features")[1].split("##")[0]
        assert "audit endpoint" in features and "slack" not in features
        assert "## Not yet implemented" in readme and "- slack alerts" in readme
        assert "pip install -r requirements.txt" in readme and "`POST /trigger-audit` | `api/server.py`" in readme
        assert "npm install" not in readme, "package.json is not at the root"

    def test_readmeWithoutAlignmentHasNoFeatureClaims(self):
        assert "## Features" not in draftReadme("app", INDEX, ())

    def test_onlyReadmeAndLicenseWritable(self):
        with pytest.raises(ValidationError):
            FileChange(path=".github/workflows/x.yml", newContent="x")
        assert FileChange(path="LICENSE", newContent="x")

    def test_licenseTemplateFilled(self):
        text = fillLicenseTemplate("Copyright (c) [year] [fullname]", 2026, "Team Zero")
        assert text == "Copyright (c) 2026 Team Zero"

    @pytest.mark.parametrize("holder", ["", "x" * 101, "bad\nname"])
    def test_licenseHolderValidated(self, holder):
        with pytest.raises(ValueError):
            fillLicenseTemplate("[fullname]", 2026, holder)

    def test_readmeLinksExtractedAndMarked(self):
        readme = "See [demo](https://demo.example/v) and https://slides.example/s. ![img](https://img.example/a.png)"
        assert extractReadmeLinks(readme) == ["https://demo.example/v", "https://slides.example/s"]
        fixed = removeLinks(readme, {"https://demo.example/v"})
        assert "demo (link removed: unreachable)" in fixed and "https://slides.example/s" in fixed

    def test_linkRemediationOnlyWhenBroken(self):
        ok = EvidenceItem(evidenceId="r1", url="https://a.example", method=EvidenceMethod.HEADLESS_BROWSER, finalStatus=200,
                          authWallDetected=False, status=CheckStatus.PASS, checkedAt=utcNow())
        assert readmeLinksRemediation("x", [ok]) is None
        bad = ok.model_copy(update={"status": CheckStatus.FAIL, "detail": "Final status 404"})
        assert readmeLinksRemediation("[a](https://a.example)", [bad]).changes[0].path == "README.md"

    def test_featureGuidanceIsAdviceOnly(self):
        guidance = featureGuidance((DONE, MISSING), INDEX)
        assert not guidance.applicable and not guidance.changes and "slack alerts" in guidance.advice[0]
        assert featureGuidance((DONE,), INDEX) is None


def thinRepoTools(readme="# App", license_status="FAIL"):
    tools = fakeTools()

    def auditRepository(repo: str):
        payload = repoAuditPayload()
        payload.update(readmeStatus="FAIL" if len(readme) < 200 else "PASS", readmeText=readme,
                       licenseStatus=license_status, licenseSpdx=None if license_status == "FAIL" else "MIT")
        return payload

    tools["auditRepository"] = StructuredTool.from_function(auditRepository, name="auditRepository", description="x")
    return tools


class TestAgentDrafts:
    def test_thinRepoGetsLicenseAndReadmeDrafts(self):
        _, summary, _ = runAudit(makeProject(), "pkt-1", deps(thinRepoTools()), utcNow())
        kinds = [r.kind for r in summary.remediations]
        assert kinds == [RemediationKind.ADD_LICENSE, RemediationKind.IMPROVE_README]
        readme = summary.remediations[1].changes[0].newContent
        assert "link auditor" in readme and "`audit.py`" in readme

    def test_brokenReadmeLinksAudited(self):
        readme = "# App\n" + "Docs. " * 50 + "\nDemo: [video](https://video.example/private)\n"
        tools = thinRepoTools(readme=readme, license_status="PASS")
        tools["auditLink"] = fakeTools(linkAuthWall=True)["auditLink"]
        packet, summary, _ = runAudit(makeProject(links=()), "pkt-1", deps(tools), utcNow())
        assert [r.kind for r in summary.remediations] == [RemediationKind.FIX_README_LINKS]
        assert any(e.evidenceId == "readmeLink1" for e in packet.evidence)

    def test_haltedAuditDraftsNothing(self):
        _, summary, _ = runAudit(makeProject(), "pkt-1", deps(fakeTools(pageReachable=False), threshold=5), utcNow())
        assert summary.halted and summary.remediations == ()


# ---------------------------------------------------------------- GitHub writer


def writerTransport(failOn=None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        calls.append((method, path, json.loads(request.content) if request.content else None))
        if failOn and failOn == (method, path):
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if (method, path) == ("GET", "/licenses/mit"):
            return httpx.Response(200, json={"body": "MIT License\n\nCopyright (c) [year] [fullname]\n"})
        if (method, path) == ("GET", "/repos/acme/app"):
            return httpx.Response(200, json={"default_branch": "main"})
        if (method, path) == ("GET", "/repos/acme/app/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "abc123"}})
        if (method, path) == ("POST", "/repos/acme/app/git/refs"):
            return httpx.Response(201, json={})
        if method == "GET" and path == "/repos/acme/app/contents/README.md":
            return httpx.Response(200, json={"sha": "oldsha"})
        if method == "GET" and path.startswith("/repos/acme/app/contents/"):
            return httpx.Response(404, json={"message": "Not Found"})
        if method == "PUT" and path.startswith("/repos/acme/app/contents/"):
            return httpx.Response(201, json={})
        if (method, path) == ("POST", "/repos/acme/app/pulls"):
            return httpx.Response(201, json={"html_url": "https://github.com/acme/app/pull/7"})
        return httpx.Response(500)

    return httpx.MockTransport(handler), calls


def makeWriter(failOn=None):
    transport, calls = writerTransport(failOn)
    return GitHubWriter("write-token", httpx.Client(base_url="https://api.github.com", transport=transport)), calls


class TestWriter:
    def test_opensPullRequestOnNewBranchNeverDefault(self):
        writer, calls = makeWriter()
        url = writer.openPullRequest("acme/app", "improveReadme", "Improve README", "body",
                                     (FileChange(path="README.md", newContent="# New"),))
        assert url == "https://github.com/acme/app/pull/7"
        branch = next(c[2]["ref"] for c in calls if c[1].endswith("/git/refs"))
        assert branch.startswith("refs/heads/shipproof/improveReadme-")
        put = next(c[2] for c in calls if c[0] == "PUT")
        assert put["branch"] != "main" and put["sha"] == "oldsha" and base64.b64decode(put["content"]) == b"# New"
        assert not any(c[0] == "PUT" and "merge" in c[1] for c in calls)

    def test_newFileHasNoSha(self):
        writer, calls = makeWriter()
        writer.openPullRequest("acme/app", "addLicense", "Add license", "b", (FileChange(path="LICENSE", newContent="MIT"),))
        assert "sha" not in next(c[2] for c in calls if c[0] == "PUT")

    def test_permissionErrorExplained(self):
        writer, _ = makeWriter(failOn=("POST", "/repos/acme/app/git/refs"))
        with pytest.raises(GitHubError, match="write access"):
            writer.openPullRequest("acme/app", "x", "t", "b", (FileChange(path="README.md", newContent="x"),))

    def test_licenseKeyWhitelist(self):
        writer, _ = makeWriter()
        assert "[fullname]" in writer.licenseTemplate("mit")
        with pytest.raises(GitHubError):
            writer.licenseTemplate("../../user")

    def test_emptyTokenRejected(self):
        with pytest.raises(GitHubError):
            GitHubWriter("")


# ---------------------------------------------------------------- API


@pytest.fixture
def remediationApi(tmp_path):
    @contextmanager
    def toolbox():
        yield thinRepoTools()

    created = []

    def writerFactory():
        writer, calls = makeWriter()
        created.append(calls)
        return writer

    app = createApp(Settings(apiToken=TOKEN, dataFile=tmp_path / "d.json", confusionThreshold=40),
                    toolboxFactory=toolbox, llmFactory=lambda: (RoutingLlm(), None), writerFactory=writerFactory)
    with TestClient(app) as client:
        client.post("/projects", json={"projectId": "demo", "repo": "acme/app", "proposalText": "We will build a live link auditor using a headless browser for teams."}, headers=AUTH)
        packetId = client.post("/projects/demo/trigger-audit", headers=AUTH).json()["packetId"]
        waitForPending(client, packetId)
        client.packetId, client.writerCalls = packetId, created
        yield client


class TestRemediationApi:
    def test_draftsVisibleInPacket(self, remediationApi):
        summary = remediationApi.get(f"/packets/{remediationApi.packetId}", headers=AUTH).json()["summary"]
        assert [r["remediationId"] for r in summary["remediations"]] == ["addLicense", "improveReadme"]
        assert all(r["status"] == "PROPOSED" for r in summary["remediations"])

    def test_applyReadmeOpensPrOnce(self, remediationApi):
        path = f"/packets/{remediationApi.packetId}/remediations/improveReadme/apply"
        applied = remediationApi.post(path, json={}, headers=AUTH)
        assert applied.status_code == 200 and applied.json()["pullRequestUrl"] == "https://github.com/acme/app/pull/7"
        assert remediationApi.post(path, json={}, headers=AUTH).status_code == 409

    def test_licenseNeedsHumanChoice(self, remediationApi):
        path = f"/packets/{remediationApi.packetId}/remediations/addLicense/apply"
        assert remediationApi.post(path, json={}, headers=AUTH).status_code == 409
        applied = remediationApi.post(path, json={"licenseKey": "mit", "copyrightHolder": "Team Zero"}, headers=AUTH)
        assert applied.json()["status"] == "APPLIED"
        put = next(c[2] for c in remediationApi.writerCalls[-1] if c[0] == "PUT")
        assert "Team Zero" in base64.b64decode(put["content"]).decode() and str(utcNow().year) in base64.b64decode(put["content"]).decode()

    def test_badLicenseKeyIsGatewayError(self, remediationApi):
        response = remediationApi.post(f"/packets/{remediationApi.packetId}/remediations/addLicense/apply",
                                       json={"licenseKey": "proprietary", "copyrightHolder": "x"}, headers=AUTH)
        assert response.status_code == 502
        assert remediationApi.get(f"/packets/{remediationApi.packetId}", headers=AUTH).json()["summary"]["remediations"][0]["status"] == "PROPOSED"

    def test_dismissAndAuth(self, remediationApi):
        path = f"/packets/{remediationApi.packetId}/remediations/improveReadme"
        assert remediationApi.post(f"{path}/apply", json={}).status_code == 401
        assert remediationApi.post(f"{path}/dismiss", headers=AUTH).json()["status"] == "DISMISSED"
        assert remediationApi.post(f"{path}/apply", json={}, headers=AUTH).status_code == 409

    def test_unknownRemediation(self, remediationApi):
        assert remediationApi.post(f"/packets/{remediationApi.packetId}/remediations/nope/apply", json={}, headers=AUTH).status_code == 404
