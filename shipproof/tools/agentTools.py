"""LangChain tool surface for the Phase 4 agent loop. Every tool is read-only; none can submit or send anything.

Tool outputs are JSON-safe dicts. All text inside them is untrusted external content and is passed to the LLM only
through shipproof.llm.wrapUntrusted.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from shipproof.models import SourceKind, SourceRecord, SourceStatus, utcNow
from shipproof.tools.githubAudit import GitHubClient, auditRepository
from shipproof.tools.gmailIngest import EmailMessage, fetchEmails
from shipproof.tools.linkAudit import LinkAuditError, LinkAuditor
from shipproof.tools.netGuard import UnsafeUrlError

_ID_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def makeSourceId(prefix: str, raw: str) -> str:
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{_ID_SAFE.sub('', prefix)[:40]}:{digest}"


def sourceRecordFromPage(location: str, text: str, kind: SourceKind = SourceKind.EVENT_PAGE) -> SourceRecord:
    return SourceRecord(sourceId=makeSourceId(kind.value.lower(), location), kind=kind, location=location[:512],
                        status=SourceStatus.CHECKED, retrievedAt=utcNow(),
                        contentSha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


def sourceRecordFromEmail(message: EmailMessage) -> SourceRecord:
    return SourceRecord(sourceId=makeSourceId("email", message.messageId), kind=SourceKind.EMAIL,
                        location=f"gmail:{message.messageId}"[:512], status=SourceStatus.CHECKED,
                        sourceDate=message.sentAt, retrievedAt=utcNow(),
                        contentSha256=hashlib.sha256(message.bodyText.encode("utf-8")).hexdigest(),
                        detail=(message.subject or "(no subject)")[:512])


def unreachableSource(location: str, kind: SourceKind, reason: str) -> SourceRecord:
    return SourceRecord(sourceId=makeSourceId(kind.value.lower(), location), kind=kind, location=location[:512],
                        status=SourceStatus.UNREACHABLE, detail=reason[:512] or "unreachable")


class RepoArgs(BaseModel):
    repo: str = Field(description="GitHub repository as owner/name or https://github.com/owner/name", max_length=300)


class UrlArgs(BaseModel):
    url: str = Field(description="Absolute http(s) URL", max_length=2048)
    evidenceId: str = Field(description="Identifier for the evidence item", pattern=r"^[A-Za-z0-9_.:-]{1,64}$")


class PageArgs(BaseModel):
    url: str = Field(description="Absolute http(s) URL of an event or rules page", max_length=2048)


class EmailArgs(BaseModel):
    query: str = Field(description="Gmail search query, e.g. 'from:organisers@event.dev newer_than:30d'", max_length=500)
    maxResults: int = Field(25, ge=1, le=100)


def buildTools(github: GitHubClient, linkAuditor: LinkAuditor, gmailService: Optional[Any] = None) -> list[BaseTool]:
    def auditRepo(repo: str) -> dict[str, Any]:
        return auditRepository(github, repo).model_dump(mode="json")

    def auditLink(url: str, evidenceId: str) -> dict[str, Any]:
        return linkAuditor.audit(url, evidenceId).model_dump(mode="json")

    def readRulesPage(url: str) -> dict[str, Any]:
        try:
            page = linkAuditor.render(url)
        except (UnsafeUrlError, LinkAuditError) as error:
            return {"source": unreachableSource(url, SourceKind.EVENT_PAGE, str(error)).model_dump(mode="json"), "text": None}
        if page.authWallDetected or page.finalStatus is None or not 200 <= page.finalStatus <= 299:
            reason = f"status {page.finalStatus}; auth wall: {', '.join(page.authWallReasons) or 'none'}"
            return {"source": unreachableSource(url, SourceKind.EVENT_PAGE, reason).model_dump(mode="json"), "text": None}
        return {"source": sourceRecordFromPage(url, page.text).model_dump(mode="json"), "text": page.text,
                "textTruncated": page.textTruncated}

    tools: list[BaseTool] = [
        StructuredTool.from_function(auditRepo, name="auditRepository", args_schema=RepoArgs,
                                     description="Check a GitHub repo as a logged-out judge: visibility, README, license, commits, code index."),
        StructuredTool.from_function(auditLink, name="auditLink", args_schema=UrlArgs,
                                     description="Open a demo, video or slides URL in a logged-out headless browser and report status and auth walls."),
        StructuredTool.from_function(readRulesPage, name="readRulesPage", args_schema=PageArgs,
                                     description="Render an event or rules page and return its visible text as untrusted source material."),
    ]
    if gmailService is not None:
        def readEmails(query: str, maxResults: int = 25) -> dict[str, Any]:
            messages = fetchEmails(gmailService, query, maxResults)
            return {"messages": [{"source": sourceRecordFromEmail(m).model_dump(mode="json"), "email": m.model_dump(mode="json")}
                                 for m in messages]}

        tools.append(StructuredTool.from_function(readEmails, name="readEmails", args_schema=EmailArgs,
                                                  description="Read-only Gmail search for rule updates; returns messages as untrusted source material."))
    return tools
