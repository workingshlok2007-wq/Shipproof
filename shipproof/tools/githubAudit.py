"""GitHub repository audit: public visibility (as a logged-out judge), README, license, commits, code index.

Rate limits: 60 requests/hour unauthenticated, 5000/hour with GITHUB_TOKEN. One full audit uses at most 6 requests.
The visibility probe is always unauthenticated, because a token would see private repos the judges cannot.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Optional
from urllib.parse import quote

import httpx
from pydantic import Field

from shipproof.models import CheckStatus, EntityId, EvidenceItem, EvidenceMethod, FrozenModel, utcNow
from shipproof.prompts import CodeIndex
from shipproof.tools.codeIndex import CodeIndexError, buildCodeIndexFromTarball

GITHUB_API = "https://api.github.com"
API_VERSION = "2022-11-28"
MIN_README_CHARS = 200
MAX_README_CHARS = 200_000
MAX_COMMITS = 100
MAX_COMMIT_MESSAGE_CHARS = 2000
MAX_TARBALL_BYTES = 50 * 1024 * 1024
NO_LICENSE_IDS = frozenset({None, "", "NOASSERTION"})

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_REPO = r"[A-Za-z0-9._-]{1,100}"
_SLUG = re.compile(rf"^({_OWNER})/({_REPO})$")
_URL = re.compile(rf"^https?://(?:www\.)?github\.com/({_OWNER})/({_REPO}?)(?:\.git)?/?$")


class GitHubError(RuntimeError):
    pass


class GitHubRateLimitError(GitHubError):
    def __init__(self, resetAt: Optional[datetime]) -> None:
        super().__init__(f"GitHub rate limit exhausted; resets at {resetAt.isoformat() if resetAt else 'unknown'}")
        self.resetAt = resetAt


def parseRepoSlug(value: str) -> tuple[str, str]:
    value = value.strip()
    match = _SLUG.match(value) or _URL.match(value)
    if not match:
        raise GitHubError(f"not a GitHub repository reference: {value[:200]!r}")
    owner, repo = match.group(1), match.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    if repo in (".", "..") or not repo:
        raise GitHubError("invalid repository name")
    return owner, repo


class RepoAudit(FrozenModel):
    owner: str
    repo: str
    visibilityEvidence: EvidenceItem
    isPubliclyVisible: bool
    readmeStatus: CheckStatus
    readmeText: Optional[str] = None
    licenseStatus: CheckStatus
    licenseSpdx: Optional[str] = None
    defaultBranch: Optional[str] = None
    commitMessages: tuple[str, ...] = ()
    codeIndex: Optional[CodeIndex] = None
    skippedArchiveEntries: Annotated[int, Field(ge=0)] = 0
    issues: tuple[str, ...] = ()


class GitHubClient:
    def __init__(self, token: Optional[str] = None, httpClient: Optional[httpx.Client] = None, timeoutSeconds: float = 20.0) -> None:
        if token is not None and (not token or token.strip() != token):
            raise GitHubError("GITHUB_TOKEN is empty or has surrounding whitespace")
        self._token = token
        self._http = httpClient or httpx.Client(base_url=GITHUB_API, timeout=timeoutSeconds, follow_redirects=True)

    @property
    def hasToken(self) -> bool:
        return self._token is not None

    def close(self) -> None:
        self._http.close()

    def _headers(self, authenticated: bool, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": API_VERSION, "User-Agent": "shipproof-auditor"}
        if authenticated and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _raiseIfRateLimited(response: httpx.Response) -> None:
        if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset")
            resetAt = datetime.fromtimestamp(int(reset), tz=timezone.utc) if reset and reset.isdigit() else None
            raise GitHubRateLimitError(resetAt)

    def get(self, path: str, authenticated: bool = True, accept: str = "application/vnd.github+json",
            params: Optional[dict[str, str]] = None) -> httpx.Response:
        try:
            response = self._http.get(path, headers=self._headers(authenticated, accept), params=params)
        except httpx.HTTPError as error:
            raise GitHubError(f"GitHub request failed: {type(error).__name__}: {error}") from error
        self._raiseIfRateLimited(response)
        return response

    def send(self, method: str, path: str, body: Optional[dict] = None) -> httpx.Response:
        try:
            response = self._http.request(method, path, headers=self._headers(True, "application/vnd.github+json"), json=body)
        except httpx.HTTPError as error:
            raise GitHubError(f"GitHub {method} failed: {type(error).__name__}: {error}") from error
        self._raiseIfRateLimited(response)
        return response

    def downloadTarball(self, path: str) -> bytes:
        chunks: list[bytes] = []
        size = 0
        try:
            with self._http.stream("GET", path, headers=self._headers(True, "application/vnd.github+json")) as response:
                self._raiseIfRateLimited(response)
                if response.status_code != 200:
                    raise GitHubError(f"tarball download returned {response.status_code}")
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_TARBALL_BYTES:
                        raise GitHubError(f"repository archive exceeds {MAX_TARBALL_BYTES} bytes")
                    chunks.append(chunk)
        except httpx.HTTPError as error:
            raise GitHubError(f"tarball download failed: {type(error).__name__}: {error}") from error
        return b"".join(chunks)


def _json(response: httpx.Response, expected: type) -> object:
    try:
        payload = response.json()
    except ValueError as error:
        raise GitHubError(f"GitHub returned invalid JSON from {response.request.url.path}") from error
    if not isinstance(payload, expected):
        raise GitHubError(f"GitHub returned unexpected JSON shape from {response.request.url.path}")
    return payload


def _repoPath(owner: str, repo: str) -> str:
    return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


def auditRepository(client: GitHubClient, repoRef: str, evidenceId: EntityId = "repoVisibility") -> RepoAudit:
    owner, repo = parseRepoSlug(repoRef)
    basePath = _repoPath(owner, repo)
    publicUrl = f"https://github.com/{owner}/{repo}"
    issues: list[str] = []

    anonymous = client.get(basePath, authenticated=False)
    if anonymous.status_code == 200:
        metadata = _json(anonymous, dict)
        isPublic = metadata.get("private") is False  # type: ignore[union-attr]
    elif anonymous.status_code == 404:
        isPublic = False
        authed = client.get(basePath) if client.hasToken else None
        if authed is not None and authed.status_code == 200:
            metadata = _json(authed, dict)
            issues.append("Repository exists but is not visible to logged-out judges.")
        else:
            issues.append("Repository not found, or private and no token was supplied to confirm.")
            metadata = None
    else:
        raise GitHubError(f"unexpected status {anonymous.status_code} probing {owner}/{repo}")

    visibilityEvidence = EvidenceItem(
        evidenceId=evidenceId, url=publicUrl, method=EvidenceMethod.GITHUB_API,
        finalStatus=anonymous.status_code, authWallDetected=False,
        status=CheckStatus.PASS if isPublic else CheckStatus.FAIL, checkedAt=utcNow(),
        detail="Public to unauthenticated users." if isPublic else issues[-1],
    )
    if metadata is None:
        return RepoAudit(owner=owner, repo=repo, visibilityEvidence=visibilityEvidence, isPubliclyVisible=False,
                         readmeStatus=CheckStatus.UNVERIFIED, licenseStatus=CheckStatus.UNVERIFIED, issues=tuple(issues))

    defaultBranch = metadata.get("default_branch")
    if not isinstance(defaultBranch, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,255}", defaultBranch):
        raise GitHubError("repository metadata has an invalid default_branch")

    readmeText: Optional[str] = None
    readmeResponse = client.get(f"{basePath}/readme", accept="application/vnd.github.raw+json")
    if readmeResponse.status_code == 200:
        readmeText = readmeResponse.text[:MAX_README_CHARS]
        populated = len(readmeText.strip()) >= MIN_README_CHARS
        readmeStatus = CheckStatus.PASS if populated else CheckStatus.FAIL
        if not populated:
            issues.append(f"README has fewer than {MIN_README_CHARS} characters.")
    elif readmeResponse.status_code == 404:
        readmeStatus = CheckStatus.FAIL
        issues.append("No README found.")
    else:
        raise GitHubError(f"unexpected status {readmeResponse.status_code} fetching README")

    licenseInfo = metadata.get("license") or {}
    licenseSpdx = licenseInfo.get("spdx_id") if isinstance(licenseInfo, dict) else None
    if licenseSpdx in NO_LICENSE_IDS:
        licenseStatus, licenseSpdx = CheckStatus.FAIL, None
        issues.append("No recognised open-source license detected.")
    else:
        licenseStatus = CheckStatus.PASS

    commitsResponse = client.get(f"{basePath}/commits", params={"per_page": str(MAX_COMMITS)})
    commitMessages: tuple[str, ...] = ()
    if commitsResponse.status_code == 200:
        commitMessages = tuple(
            str(item.get("commit", {}).get("message", ""))[:MAX_COMMIT_MESSAGE_CHARS]
            for item in _json(commitsResponse, list)[:MAX_COMMITS] if isinstance(item, dict)
        )
    elif commitsResponse.status_code == 409:
        issues.append("Repository has no commits.")
    else:
        raise GitHubError(f"unexpected status {commitsResponse.status_code} fetching commits")

    codeIndex: Optional[CodeIndex] = None
    skipped = 0
    if commitMessages:
        try:
            codeIndex, skipped = buildCodeIndexFromTarball(client.downloadTarball(f"{basePath}/tarball/{quote(defaultBranch, safe='/')}"))
        except CodeIndexError as error:
            raise GitHubError(str(error)) from error

    return RepoAudit(
        owner=owner, repo=repo, visibilityEvidence=visibilityEvidence, isPubliclyVisible=isPublic,
        readmeStatus=readmeStatus, readmeText=readmeText, licenseStatus=licenseStatus, licenseSpdx=licenseSpdx,
        defaultBranch=defaultBranch, commitMessages=commitMessages, codeIndex=codeIndex,
        skippedArchiveEntries=skipped, issues=tuple(issues),
    )
