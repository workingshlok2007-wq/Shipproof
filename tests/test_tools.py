import base64
import io
import socket
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from shipproof.models import CheckStatus, SourceStatus
from shipproof.tools import netGuard
from shipproof.tools.agentTools import buildTools, sourceRecordFromEmail
from shipproof.tools.codeIndex import CodeIndexError, buildCodeIndexFromTarball, extractSymbols
from shipproof.tools.githubAudit import GitHubClient, GitHubError, GitHubRateLimitError, auditRepository, parseRepoSlug
from shipproof.tools.gmailIngest import GmailError, fetchEmails, htmlToText, parseMessage
from shipproof.tools.linkAudit import LinkAuditError, LinkAuditor, detectAuthWall
from shipproof.tools.netGuard import UnsafeUrlError, assertPublicUrl


# ---------------------------------------------------------------- netGuard

@pytest.fixture
def fakeDns(monkeypatch):
    table = {"public.example": "93.184.216.34", "internal.example": "10.0.0.5", "meta.example": "169.254.169.254",
             "v6local.example": "::1"}

    def fakeGetaddrinfo(host, port, type=0):
        if host not in table:
            raise socket.gaierror("not found")
        family = socket.AF_INET6 if ":" in table[host] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (table[host], port))]

    netGuard._resolve.cache_clear()
    monkeypatch.setattr(netGuard.socket, "getaddrinfo", fakeGetaddrinfo)
    yield
    netGuard._resolve.cache_clear()


class TestNetGuard:
    def test_publicAllowed(self, fakeDns):
        assert assertPublicUrl("https://public.example/x")

    @pytest.mark.parametrize("url", [
        "http://internal.example/", "http://meta.example/latest/meta-data", "http://v6local.example/",
        "file:///etc/passwd", "ftp://public.example/", "https://user:pw@public.example/", "https://", "http://nohost.example/",
        "http://public.example:99999/",
    ])
    def test_unsafeRejected(self, fakeDns, url):
        with pytest.raises(UnsafeUrlError):
            assertPublicUrl(url)

    def test_privateAllowedOnlyWhenOptedIn(self, fakeDns):
        assert assertPublicUrl("http://internal.example/", allowPrivateNetwork=True)


# ---------------------------------------------------------------- codeIndex

def makeTarball(files, prefix="owner-repo-abc123/", extra=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info in extra or ():
            archive.addfile(info)
    return buffer.getvalue()


class TestCodeIndex:
    def test_extractSymbolsAcrossLanguages(self):
        source = (
            "async def triggerAudit():\n    pass\nclass AuditGraph:\n    pass\n"
            "export function renderLedger() {}\nexport const approve = async (id) => {}\n"
            "func (s *Server) Serve() {}\npub fn parse() {}\n"
            "@app.post(\"/trigger-audit\")\nrouter.get('/packets/:id', h)\n"
        )
        symbols = extractSymbols(source)
        for expected in ("triggerAudit", "AuditGraph", "renderLedger", "approve", "Serve", "parse",
                         "POST /trigger-audit", "GET /packets/:id"):
            assert expected in symbols

    def test_buildIndexSkipsVendorAndLinks(self):
        link = tarfile.TarInfo("owner-repo-abc123/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        data = makeTarball({"app/main.py": "def runAudit():\n  pass\n", "node_modules/x/index.js": "function bad(){}",
                            "README.md": "# hi"}, extra=[link])
        index, skipped = buildCodeIndexFromTarball(data)
        paths = {e.path for e in index.entries}
        assert paths == {"app/main.py", "README.md"} and skipped == 1
        assert index.hasSymbol("runAudit") and index.hasPath("README.md")

    def test_traversalRejected(self):
        data = makeTarball({"../../etc/x.py": "def a(): pass"}, prefix="")
        index, skipped = buildCodeIndexFromTarball(data)
        assert index.entries == () and skipped == 1

    def test_oversizeSourceIndexedWithoutSymbols(self):
        index, skipped = buildCodeIndexFromTarball(makeTarball({"big.py": "def a(): pass\n" + "#" * 600_000}))
        assert index.entries[0].symbols == () and skipped == 1

    def test_corruptArchive(self):
        with pytest.raises(CodeIndexError):
            buildCodeIndexFromTarball(b"not a tarball")


# ---------------------------------------------------------------- githubAudit

README = "# ShipProof\n" + "Agentic submission reliability. " * 20


def githubTransport(anonymousStatus=200, private=False, readmeStatus=200, readme=README, license="MIT",
                    commitsStatus=200, rateLimited=False, authedStatus=200):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        authed = "authorization" in request.headers
        calls.append((path, authed))
        if rateLimited:
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"})
        if path == "/repos/acme/app":
            if not authed and anonymousStatus != 200:
                return httpx.Response(anonymousStatus, json={"message": "Not Found"})
            if authed and anonymousStatus == 404 and authedStatus != 200:
                return httpx.Response(authedStatus)
            return httpx.Response(200, json={"private": private, "default_branch": "main",
                                             "license": {"spdx_id": license} if license else None})
        if path == "/repos/acme/app/readme":
            return httpx.Response(readmeStatus, text=readme)
        if path == "/repos/acme/app/commits":
            return httpx.Response(commitsStatus, json=[{"commit": {"message": "Add audit loop"}}])
        if path == "/repos/acme/app/tarball/main":
            return httpx.Response(302, headers={"location": "https://codeload.github.com/acme/app/legacy.tar.gz/main"})
        if request.url.host == "codeload.github.com":
            assert not authed, "token must not leak to redirect host"
            return httpx.Response(200, content=makeTarball({"api/server.py": "@app.post('/trigger-audit')\ndef triggerAudit(): pass\n"}))
        return httpx.Response(500)

    return httpx.MockTransport(handler), calls


def githubClient(token=None, **kwargs):
    transport, calls = githubTransport(**kwargs)
    http = httpx.Client(base_url="https://api.github.com", transport=transport, follow_redirects=True)
    return GitHubClient(token=token, httpClient=http), calls


class TestGitHubAudit:
    @pytest.mark.parametrize("ref,expected", [
        ("acme/app", ("acme", "app")), ("https://github.com/acme/app.git", ("acme", "app")),
        ("https://github.com/acme/app/", ("acme", "app")),
    ])
    def test_parseSlug(self, ref, expected):
        assert parseRepoSlug(ref) == expected

    @pytest.mark.parametrize("ref", ["acme", "acme/app/extra", "https://evil.com/acme/app", "-bad/app", "acme/..", "acme/app?x=1"])
    def test_parseSlugRejects(self, ref):
        with pytest.raises(GitHubError):
            parseRepoSlug(ref)

    def test_publicRepoFullAudit(self):
        client, calls = githubClient(token="tkn")
        audit = auditRepository(client, "acme/app")
        assert audit.isPubliclyVisible and audit.visibilityEvidence.status is CheckStatus.PASS
        assert audit.readmeStatus is CheckStatus.PASS and audit.licenseStatus is CheckStatus.PASS
        assert audit.commitMessages == ("Add audit loop",)
        assert audit.codeIndex.hasSymbol("POST /trigger-audit")
        assert calls[0] == ("/repos/acme/app", False), "visibility probe must be unauthenticated"

    def test_privateRepoFlaggedButAuditedWithToken(self):
        client, _ = githubClient(token="tkn", anonymousStatus=404)
        audit = auditRepository(client, "acme/app")
        assert not audit.isPubliclyVisible and audit.visibilityEvidence.status is CheckStatus.FAIL
        assert audit.codeIndex is not None and "not visible to logged-out judges" in audit.issues[0]

    def test_missingRepoWithoutToken(self):
        audit = auditRepository(githubClient(anonymousStatus=404)[0], "acme/app")
        assert audit.readmeStatus is CheckStatus.UNVERIFIED and audit.codeIndex is None

    def test_thinReadmeAndNoLicense(self):
        audit = auditRepository(githubClient(readme="# x", license="NOASSERTION")[0], "acme/app")
        assert audit.readmeStatus is CheckStatus.FAIL and audit.licenseStatus is CheckStatus.FAIL

    def test_missingReadme(self):
        assert auditRepository(githubClient(readmeStatus=404)[0], "acme/app").readmeStatus is CheckStatus.FAIL

    def test_emptyRepo(self):
        audit = auditRepository(githubClient(commitsStatus=409)[0], "acme/app")
        assert audit.codeIndex is None and "no commits" in audit.issues[-1]

    def test_rateLimit(self):
        with pytest.raises(GitHubRateLimitError) as error:
            auditRepository(githubClient(rateLimited=True)[0], "acme/app")
        assert error.value.resetAt is not None

    def test_unexpectedStatus(self):
        with pytest.raises(GitHubError):
            auditRepository(githubClient(anonymousStatus=500)[0], "acme/app")

    def test_blankToken(self):
        with pytest.raises(GitHubError):
            GitHubClient(token=" ")


# ---------------------------------------------------------------- gmailIngest

def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def rawMessage(messageId="m1", internalDate="1762800000000", plain=None, htmlBody=None):
    parts = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(plain)}})
    if htmlBody is not None:
        parts.append({"mimeType": "text/html", "body": {"data": b64(htmlBody)}})
    return {"id": messageId, "internalDate": internalDate, "payload": {
        "mimeType": "multipart/alternative", "parts": parts,
        "headers": [{"name": "From", "value": "Organisers <org@event.dev>"}, {"name": "Subject", "value": "Deadline moved"},
                    {"name": "Message-ID", "value": "<abc@event.dev>"}]}}


class FakeGmail:
    def __init__(self, messages):
        self.messages = {m["id"]: m for m in messages}

    def list(self, userId, q, maxResults):
        return type("Exec", (), {"execute": lambda _: {"messages": [{"id": k} for k in self.messages]}})()

    def get(self, userId, id, format):
        return type("Exec", (), {"execute": lambda _: self.messages[id]})()


class FakeGmailService:
    def __init__(self, messages):
        self._store = FakeGmail(messages)

    def users(self):
        return self

    def messages(self):
        return self._store


class TestGmail:
    def test_parsePrefersPlain(self):
        message = parseMessage(rawMessage(plain="Deadline is now 12 Nov.", htmlBody="<p>ignored</p>"))
        assert message.bodyText == "Deadline is now 12 Nov." and message.sentAt.tzinfo is not None
        assert message.rfcMessageId == "<abc@event.dev>" and message.subject == "Deadline moved"

    def test_htmlFallbackStripsScripts(self):
        message = parseMessage(rawMessage(htmlBody="<script>alert(1)</script><p>Rules &amp; deadlines</p>"))
        assert message.bodyText == "Rules & deadlines"

    def test_malformedRejected(self):
        with pytest.raises(GmailError):
            parseMessage({"id": "x"})

    def test_fetchSortedNewestFirst(self):
        service = FakeGmailService([rawMessage("old", "1000", plain="a"), rawMessage("new", "2000", plain="b")])
        assert [m.messageId for m in fetchEmails(service, "from:org@event.dev")] == ["new", "old"]

    @pytest.mark.parametrize("query,limit", [("", 5), ("x" * 501, 5), ("ok\x00", 5), ("ok", 0), ("ok", 101)])
    def test_fetchValidatesInput(self, query, limit):
        with pytest.raises(GmailError):
            fetchEmails(FakeGmailService([]), query, limit)

    def test_emailSourceRecordCarriesDate(self):
        record = sourceRecordFromEmail(parseMessage(rawMessage(plain="x")))
        assert record.status is SourceStatus.CHECKED and record.sourceDate is not None

    def test_htmlToText(self):
        assert htmlToText("<style>p{}</style><b>A</b>&lt;B&gt;") == "A <B>"


# ---------------------------------------------------------------- linkAudit (real headless Chromium, local server)

PAGES = {
    "/ok": (200, "<html><body><h1>Demo</h1><p>Welcome to the demo.</p></body></html>"),
    "/private": (200, "<html><body>This video is private.</body></html>"),
    "/login": (200, "<html><body><form><input type='password'></form></body></html>"),
    "/redirect": (302, "/accounts/signin"),
    "/accounts/signin": (200, "<html><body>Please continue</body></html>"),
    "/gone": (200, "<html><body>Page not found</body></html>"),
    "/missing": (404, "<html><body>nothing</body></html>"),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = PAGES.get(self.path, (404, "no"))
        self.send_response(status)
        if status == 302:
            self.send_header("Location", body)
            body = ""
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def localServer():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def auditor():
    try:
        with LinkAuditor(allowPrivateNetwork=True, timeoutMs=10_000) as instance:
            yield instance
    except LinkAuditError as error:
        pytest.skip(f"Chromium unavailable: {error}")


class TestLinkAudit:
    def test_detectAuthWall(self):
        assert detectAuthWall("https://accounts.google.com/x", "", False)
        assert detectAuthWall("https://site.dev/", "", True) == ("form:password-field",)
        assert detectAuthWall("https://site.dev/demo", "hello", False) == ()

    @pytest.mark.parametrize("path,status,authWall", [
        ("/ok", CheckStatus.PASS, False), ("/private", CheckStatus.FAIL, True), ("/login", CheckStatus.FAIL, True),
        ("/redirect", CheckStatus.FAIL, True), ("/gone", CheckStatus.FAIL, False), ("/missing", CheckStatus.FAIL, False),
    ])
    def test_audit(self, auditor, localServer, path, status, authWall):
        evidence = auditor.audit(localServer + path, "e1")
        assert evidence.status is status and evidence.authWallDetected is authWall

    def test_privateNetworkBlockedByDefault(self, auditor, localServer):
        strict = LinkAuditor()
        strict._browser = auditor._browser  # share the running browser; only one sync Playwright per thread
        evidence = strict.audit(localServer + "/ok", "e1")
        assert evidence.status is CheckStatus.FAIL and "UnsafeUrlError" in evidence.detail

    def test_unreachableRecordedNotRaised(self, auditor):
        evidence = auditor.audit("http://127.0.0.1:1/", "e1")
        assert evidence.status is CheckStatus.FAIL and evidence.finalStatus is None

    def test_toolsInvoke(self, auditor, localServer):
        client, _ = githubClient()
        tools = {t.name: t for t in buildTools(client, auditor, FakeGmailService([rawMessage(plain="x")]))}
        assert set(tools) == {"auditRepository", "auditLink", "readRulesPage", "readEmails"}
        assert tools["auditRepository"].invoke({"repo": "acme/app"})["isPubliclyVisible"]
        assert tools["auditLink"].invoke({"url": localServer + "/ok", "evidenceId": "e1"})["status"] == "PASS"
        page = tools["readRulesPage"].invoke({"url": localServer + "/ok"})
        assert page["source"]["status"] == "CHECKED" and "Welcome" in page["text"]
        assert tools["readRulesPage"].invoke({"url": localServer + "/login"})["source"]["status"] == "UNREACHABLE"
        assert len(tools["readEmails"].invoke({"query": "deadline"})["messages"]) == 1

    def test_gmailToolOmittedWithoutService(self, auditor):
        assert "readEmails" not in {t.name for t in buildTools(githubClient()[0], auditor)}
