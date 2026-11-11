"""Deterministic demo: a local fixture event site, repository and inbox with planted problems.

Enabled with SHIPPROOF_DEMO=1. Everything is served from 127.0.0.1, so the demo runs offline. Only the
fixture origin is exempt from the private-network guard; every other private address stays blocked.

Planted problems: submitted video is private (a public one is linked from the README), an undated rules update
contradicts the dated deadline, a prompt injection on the rules page, a dead slides link in the README, no LICENSE,
and one promised feature (Slack notifications) with no code.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from langchain_core.tools import BaseTool, StructuredTool

from shipproof.agent.project import LinkTarget, Project
from shipproof.models import Actor, ActorKind, CheckStatus, EvidenceItem, EvidenceMethod, ProofCheck, Role, SourceRef, SourceRefKind, addCharterClause, utcNow
from shipproof.tools.agentTools import EmailArgs, RepoArgs, sourceRecordFromEmail
from shipproof.tools.codeIndex import buildCodeIndexFromTarball
from shipproof.tools.githubAudit import MIN_README_CHARS, RepoAudit
from shipproof.tools.gmailIngest import EmailMessage

FIXTURES = Path(__file__).parent / "fixtures"
DEMO_REPO = "demo-org/shipproof-demo-app"
DEMO_PROJECT_ID = "demo"
DEMO_PORT = 8799
OWNER = Actor(actorId="owner", kind=ActorKind.HUMAN, role=Role.OWNER)


def demoEnabled() -> bool:
    return os.environ.get("SHIPPROOF_DEMO") == "1"


def demoPort() -> int:
    try:
        port = int(os.environ.get("SHIPPROOF_DEMO_PORT", str(DEMO_PORT)))
    except ValueError as error:
        raise ValueError("SHIPPROOF_DEMO_PORT must be an integer") from error
    if not 1024 <= port <= 65535:
        raise ValueError("SHIPPROOF_DEMO_PORT must be in [1024, 65535]")
    return port


def baseUrl(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _render(path: Path, base: str) -> str:
    return path.read_text(encoding="utf-8").replace("{{BASE}}", base)


class DemoSiteServer:
    """Serves fixtures/site on 127.0.0.1 only. Unknown paths return 404 (used by the dead-link fixture)."""

    def __init__(self, port: int) -> None:
        self.base = baseUrl(port)
        pages = {f"/{p.name}": _render(p, self.base).encode("utf-8") for p in (FIXTURES / "site").glob("*.html")}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib name
                body = pages.get(self.path.split("?", 1)[0])
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body or b"<html><body><h1>404 Not Found</h1></body></html>")

            def log_message(self, *_: Any) -> None:
                pass

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as error:
            raise RuntimeError(f"demo site cannot bind 127.0.0.1:{port} ({error}); set SHIPPROOF_DEMO_PORT") from error
        self._thread = threading.Thread(target=self._server.serve_forever, name="demo-site", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def demoRepoAudit(base: str) -> dict[str, Any]:
    """Builds the repository audit from the fixture directory exactly as the GitHub tool would from a tarball."""
    repoDir = FIXTURES / "repo"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(repoDir.rglob("*")):
            if path.is_file() and path.name != "commits.txt":
                data = _render(path, base).encode("utf-8")
                info = tarfile.TarInfo(f"demo-org-shipproof-demo-app-fixture/{path.relative_to(repoDir).as_posix()}")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    codeIndex, skipped = buildCodeIndexFromTarball(buffer.getvalue())
    readme = _render(repoDir / "README.md", base)
    visibility = EvidenceItem(evidenceId="repoVisibility", url=f"https://github.com/{DEMO_REPO}", method=EvidenceMethod.GITHUB_API,
                              finalStatus=200, authWallDetected=False, status=CheckStatus.PASS, checkedAt=utcNow(),
                              detail="Public to unauthenticated users (demo fixture).")
    return RepoAudit(
        owner="demo-org", repo="shipproof-demo-app", visibilityEvidence=visibility, isPubliclyVisible=True,
        readmeStatus=CheckStatus.PASS if len(readme.strip()) >= MIN_README_CHARS else CheckStatus.FAIL, readmeText=readme,
        licenseStatus=CheckStatus.FAIL, licenseSpdx=None, defaultBranch="main",
        commitMessages=tuple((repoDir / "commits.txt").read_text(encoding="utf-8").splitlines()),
        codeIndex=codeIndex, skippedArchiveEntries=skipped, issues=("No recognised open-source license detected.",),
    ).model_dump(mode="json")


def demoEmails() -> list[EmailMessage]:
    raw = json.loads((FIXTURES / "emails.json").read_text(encoding="utf-8"))
    return [EmailMessage(messageId=m["id"], rfcMessageId=m["messageId"], sender=m["from"], subject=m["subject"], bodyText=m["body"],
                         bodyTruncated=False, sentAt=datetime.fromtimestamp(int(m["internalDate"]) / 1000, tz=timezone.utc)) for m in raw]


def _matches(message: EmailMessage, query: str) -> bool:
    """Tiny Gmail-like matcher: from:x matches the sender, other words must appear somewhere; newer_than is ignored."""
    haystack = f"{message.sender}\n{message.subject}\n{message.bodyText}".casefold()
    for term in query.casefold().split():
        if term.startswith("newer_than:"):
            continue
        if term.startswith("from:"):
            if term[5:] not in message.sender.casefold():
                return False
        elif term not in haystack:
            return False
    return True


def demoTools(base: str, realRepoTool: Optional[BaseTool]) -> list[BaseTool]:
    """Fixture GitHub (for the demo repo only) and fixture inbox. Link checks stay real (headless browser)."""
    def auditRepository(repo: str) -> dict[str, Any]:
        if repo.strip().removeprefix("https://github.com/").rstrip("/") == DEMO_REPO:
            return demoRepoAudit(base)
        if realRepoTool is None:
            raise RuntimeError("only the demo repository is available in demo mode")
        return realRepoTool.invoke({"repo": repo})

    def readEmails(query: str, maxResults: int = 25) -> dict[str, Any]:
        messages = [m for m in demoEmails() if _matches(m, query)][:maxResults]
        return {"messages": [{"source": sourceRecordFromEmail(m).model_dump(mode="json"), "email": m.model_dump(mode="json")} for m in messages]}

    return [
        StructuredTool.from_function(auditRepository, name="auditRepository", args_schema=RepoArgs,
                                     description="Check a GitHub repo as a logged-out judge (demo fixture for the demo repo)."),
        StructuredTool.from_function(readEmails, name="readEmails", args_schema=EmailArgs, description="Search the demo inbox."),
    ]


def demoProject(base: str) -> Project:
    ref = SourceRef(kind=SourceRefKind.URL, value=f"{base}/rules.html")
    charter = addCharterClause(Project(projectId=DEMO_PROJECT_ID, repo=DEMO_REPO, proposalText="x").charter, "c1",
                               "Every submission must include a public demo video under 3 minutes.", ref, OWNER)
    check = ProofCheck(checkId="p1", clauseId="c1", clauseVersion=1, authoredBy=OWNER,
                       interpretation="Any video link works as long as the video is short.")
    return Project(
        projectId=DEMO_PROJECT_ID, repo=DEMO_REPO, proposalText=(FIXTURES / "proposal.txt").read_text(encoding="utf-8").strip(),
        links=(LinkTarget(label="Demo video", url=f"{base}/video-private.html"), LinkTarget(label="Slides", url=f"{base}/slides.html")),
        rulePages=(f"{base}/rules.html",), emailQuery="from:organisers@demo.event", charter=charter, proofChecks=(check,),
    )
