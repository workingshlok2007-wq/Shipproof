"""Read-only Gmail ingestion for rule updates.

OAuth scope: gmail.readonly (a Google "restricted" scope). For the hackathon, keep the OAuth app in Testing mode
with your own address as a test user; refresh tokens then expire after 7 days.
Setup: Google Cloud console -> enable Gmail API -> OAuth client ID (type "Desktop app") -> download JSON.
Env: GMAIL_CLIENT_SECRETS (path to that JSON), GMAIL_TOKEN_FILE (where the token is cached).
The token file is a credential: it is written owner-only on Linux; on Windows file ACLs are inherited from the folder,
so keep it inside your user profile and out of the repository.
"""

from __future__ import annotations

import base64
import binascii
import html
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from shipproof.models import FrozenModel

GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
MAX_QUERY_CHARS = 500
MAX_RESULTS = 100
MAX_BODY_CHARS = 50_000
MAX_MIME_DEPTH = 10

_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class GmailError(RuntimeError):
    pass


class EmailMessage(FrozenModel):
    messageId: str
    rfcMessageId: Optional[str] = None
    sentAt: datetime
    sender: str
    subject: str
    bodyText: str
    bodyTruncated: bool


def loadGmailCredentials(clientSecretsPath: Optional[str] = None, tokenPath: Optional[str] = None) -> Any:
    """Loads cached credentials, refreshing if expired; otherwise runs the local-server OAuth consent flow once."""
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = Path(clientSecretsPath or os.environ.get("GMAIL_CLIENT_SECRETS", ""))
    token = Path(tokenPath or os.environ.get("GMAIL_TOKEN_FILE", ""))
    if not str(token) or str(token) == ".":
        raise GmailError("GMAIL_TOKEN_FILE is not set")
    credentials = None
    try:
        if token.is_file():
            credentials = Credentials.from_authorized_user_file(str(token), list(GMAIL_SCOPES))
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials or not credentials.valid:
            if not secrets.is_file():
                raise GmailError("GMAIL_CLIENT_SECRETS does not point to an OAuth client JSON file")
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), list(GMAIL_SCOPES))
            credentials = flow.run_local_server(port=0, open_browser=False)
    except (GoogleAuthError, ValueError, OSError) as error:
        raise GmailError(f"Gmail authorisation failed: {error}") from error
    _writePrivateFile(token, credentials.to_json())
    return credentials


def _writePrivateFile(path: Path, content: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        if os.name == "posix":
            os.chmod(path, 0o600)  # O_CREAT mode does not apply to a pre-existing file
    except OSError as error:
        raise GmailError(f"cannot write token file {path}: {error}") from error


def buildGmailService(credentials: Any) -> Any:
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _decodeBody(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError) as error:
        raise GmailError("malformed base64 message body") from error


def _collectParts(payload: dict[str, Any], depth: int = 0) -> tuple[list[str], list[str]]:
    if depth > MAX_MIME_DEPTH:
        raise GmailError("MIME structure nested too deeply")
    plain: list[str] = []
    htmlParts: list[str] = []
    mimeType = str(payload.get("mimeType", ""))
    data = (payload.get("body") or {}).get("data")
    if data and mimeType == "text/plain":
        plain.append(_decodeBody(data))
    elif data and mimeType == "text/html":
        htmlParts.append(_decodeBody(data))
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            childPlain, childHtml = _collectParts(part, depth + 1)
            plain += childPlain
            htmlParts += childHtml
    return plain, htmlParts


def htmlToText(markup: str) -> str:
    return re.sub(r"[ \t]+", " ", html.unescape(_TAGS.sub(" ", markup))).strip()


def parseMessage(raw: dict[str, Any]) -> EmailMessage:
    payload = raw.get("payload")
    messageId = raw.get("id")
    internalDate = raw.get("internalDate")
    if not isinstance(payload, dict) or not isinstance(messageId, str) or not str(internalDate or "").isdigit():
        raise GmailError("Gmail message missing id, payload or internalDate")
    headers = {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in payload.get("headers") or [] if isinstance(h, dict)}
    plain, htmlParts = _collectParts(payload)
    body = "\n".join(plain) if plain else htmlToText("\n".join(htmlParts))
    body = _CONTROL.sub("", body)
    return EmailMessage(
        messageId=messageId,
        rfcMessageId=headers.get("message-id") or None,
        sentAt=datetime.fromtimestamp(int(internalDate) / 1000, tz=timezone.utc),
        sender=_CONTROL.sub("", headers.get("from", ""))[:512],
        subject=_CONTROL.sub("", headers.get("subject", ""))[:512],
        bodyText=body[:MAX_BODY_CHARS],
        bodyTruncated=len(body) > MAX_BODY_CHARS,
    )


def fetchEmails(service: Any, query: str, maxResults: int = 25) -> tuple[EmailMessage, ...]:
    """Gmail search syntax, e.g. 'from:organisers@event.dev newer_than:30d'. Newest first."""
    from googleapiclient.errors import HttpError

    if not query.strip() or len(query) > MAX_QUERY_CHARS or _CONTROL.search(query):
        raise GmailError(f"query must be 1-{MAX_QUERY_CHARS} printable characters")
    if not 1 <= maxResults <= MAX_RESULTS:
        raise GmailError(f"maxResults must be in [1, {MAX_RESULTS}]")
    try:
        listing = service.users().messages().list(userId="me", q=query, maxResults=maxResults).execute()
        messages = [
            parseMessage(service.users().messages().get(userId="me", id=item["id"], format="full").execute())
            for item in listing.get("messages", []) if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
    except HttpError as error:
        raise GmailError(f"Gmail API error {error.resp.status}: {error.reason}") from error
    return tuple(sorted(messages, key=lambda m: m.sentAt, reverse=True))
