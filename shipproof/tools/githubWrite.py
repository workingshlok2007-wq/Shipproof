"""Opens a pull request for an approved remediation. Never pushes to the default branch, never merges.

Least privilege: uses GITHUB_WRITE_TOKEN only (fine-grained token scoped to the one repository with
Contents: read/write and Pull requests: read/write). The read path keeps using GITHUB_TOKEN.
"""

from __future__ import annotations

import base64
import re
import secrets
from typing import Any, Optional
from urllib.parse import quote

import httpx

from shipproof.remediation import LICENSE_KEYS, WRITABLE_PATHS, FileChange
from shipproof.tools.githubAudit import GITHUB_API, GitHubClient, GitHubError, parseRepoSlug

_BRANCH_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class GitHubWriter:
    def __init__(self, token: str, httpClient: Optional[httpx.Client] = None) -> None:
        if not token or token.strip() != token:
            raise GitHubError("GITHUB_WRITE_TOKEN is missing or has surrounding whitespace")
        self._client = GitHubClient(token=token, httpClient=httpClient or httpx.Client(base_url=GITHUB_API, timeout=20.0))

    def close(self) -> None:
        self._client.close()

    def _send(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> httpx.Response:
        return self._client.send(method, path, body)

    @staticmethod
    def _expect(response: httpx.Response, statuses: tuple[int, ...], action: str) -> dict[str, Any]:
        if response.status_code not in statuses:
            try:
                message = str(response.json().get("message", ""))[:300]
            except ValueError:
                message = response.text[:300]
            hint = " (token lacks write access to this repository?)" if response.status_code in (403, 404) else ""
            raise GitHubError(f"{action} failed with {response.status_code}: {message}{hint}")
        try:
            payload = response.json()
        except ValueError as error:
            raise GitHubError(f"{action}: invalid JSON from GitHub") from error
        if not isinstance(payload, dict):
            raise GitHubError(f"{action}: unexpected JSON shape")
        return payload

    def licenseTemplate(self, licenseKey: str) -> str:
        if licenseKey not in LICENSE_KEYS:
            raise GitHubError(f"license must be one of {', '.join(LICENSE_KEYS)}")
        payload = self._expect(self._send("GET", f"/licenses/{licenseKey}"), (200,), "Fetching license template")
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip():
            raise GitHubError("license template is empty")
        return body

    def openPullRequest(self, repoRef: str, remediationId: str, title: str, body: str, changes: tuple[FileChange, ...]) -> str:
        if not changes:
            raise GitHubError("nothing to change")
        for change in changes:
            if change.path not in WRITABLE_PATHS:
                raise GitHubError(f"refusing to write {change.path}")
        owner, repo = parseRepoSlug(repoRef)
        base = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

        metadata = self._expect(self._send("GET", base), (200,), "Reading repository")
        defaultBranch = metadata.get("default_branch")
        if not isinstance(defaultBranch, str) or not defaultBranch:
            raise GitHubError("repository has no default branch")
        ref = self._expect(self._send("GET", f"{base}/git/ref/heads/{quote(defaultBranch, safe='/')}"), (200,), "Reading default branch")
        baseSha = (ref.get("object") or {}).get("sha")
        if not isinstance(baseSha, str):
            raise GitHubError("default branch has no commit")

        branch = f"shipproof/{_BRANCH_SAFE.sub('-', remediationId)[:40]}-{secrets.token_hex(3)}"
        self._expect(self._send("POST", f"{base}/git/refs", {"ref": f"refs/heads/{branch}", "sha": baseSha}), (201,), "Creating branch")

        for change in changes:
            contentPath = f"{base}/contents/{quote(change.path)}"
            existing = self._send("GET", f"{contentPath}?ref={quote(branch, safe='')}")
            existingSha = self._expect(existing, (200,), "Reading file").get("sha") if existing.status_code == 200 else None
            if existing.status_code not in (200, 404):
                self._expect(existing, (200,), "Reading file")
            payload: dict[str, Any] = {
                "message": f"ShipProof: {title} ({change.path})",
                "content": base64.b64encode(change.newContent.encode("utf-8")).decode("ascii"),
                "branch": branch,
            }
            if isinstance(existingSha, str):
                payload["sha"] = existingSha
            self._expect(self._send("PUT", contentPath, payload), (200, 201), f"Writing {change.path}")

        pull = self._expect(self._send("POST", f"{base}/pulls", {"title": f"ShipProof: {title}", "head": branch,
                                                                  "base": defaultBranch, "body": body}), (201,), "Opening pull request")
        url = pull.get("html_url")
        if not isinstance(url, str) or not url.startswith("https://github.com/"):
            raise GitHubError("pull request created but GitHub returned no URL")
        return url
