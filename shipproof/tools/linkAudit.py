"""Judge's-vantage link audit: a fresh, logged-out headless Chromium context per URL.

Install the browser once:
  Linux/WSL/Codespaces:  python -m playwright install --with-deps chromium
  Windows:               python -m playwright install chromium
Set SHIPPROOF_CHROMIUM_PATH to use an existing Chromium binary instead.

Uses Playwright's sync API: call it from a worker thread, never inside a running asyncio event loop.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from shipproof.models import CheckStatus, EntityId, EvidenceItem, EvidenceMethod, utcNow
from urllib.parse import urlsplit as _split

from shipproof.tools.netGuard import UnsafeUrlError, assertPublicUrl, isPublicUrl


def _origin(url: str) -> str:
    try:
        parts = _split(url)
        return f"{parts.scheme}://{parts.netloc}".lower()
    except ValueError:
        return ""

DEFAULT_TIMEOUT_MS = 20_000
SETTLE_TIMEOUT_MS = 4_000
MAX_PAGE_TEXT_CHARS = 200_000

AUTH_URL_MARKERS = (
    "accounts.google.com", "login.microsoftonline.com", "login.live.com", "github.com/login",
    "/login", "/signin", "/sign-in", "/sign_in", "/auth/", "/sso", "/oauth",
)
AUTH_TEXT_MARKERS = (
    "this video is private", "video unavailable", "you need access", "request access",
    "you need permission", "sign in to continue", "log in to continue", "sign in to view",
    "login required",
)
UNAVAILABLE_TEXT_MARKERS = (
    "page not found", "404 not found", "this content isn't available", "this page isn't available",
    "file does not exist", "has been removed", "no longer available",
)


class LinkAuditError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderedPage:
    requestedUrl: str
    finalUrl: str
    finalStatus: Optional[int]
    text: str
    textTruncated: bool
    contentSha256: str
    authWallDetected: bool
    authWallReasons: tuple[str, ...]
    unavailableReasons: tuple[str, ...]
    links: tuple[str, ...] = ()


MAX_PAGE_LINKS = 100


def detectUnavailable(pageText: str) -> tuple[str, ...]:
    """Soft-404 detection: pages that return 200 but say the content is gone."""
    lowered = pageText.lower()
    return tuple(f"text:{m}" for m in UNAVAILABLE_TEXT_MARKERS if m in lowered)


def detectAuthWall(finalUrl: str, pageText: str, hasPasswordField: bool) -> tuple[str, ...]:
    reasons: list[str] = []
    parts = urlsplit(finalUrl.lower())
    location = f"{parts.netloc}{parts.path}"
    reasons += [f"url:{m}" for m in AUTH_URL_MARKERS if m in location]
    lowered = pageText.lower()
    reasons += [f"text:{m}" for m in AUTH_TEXT_MARKERS if m in lowered]
    if hasPasswordField:
        reasons.append("form:password-field")
    return tuple(reasons)


class LinkAuditor:
    """Context manager owning one browser. Each URL gets an isolated context with no cookies or storage."""

    def __init__(self, allowPrivateNetwork: bool = False, timeoutMs: int = DEFAULT_TIMEOUT_MS,
                 allowedOrigins: tuple[str, ...] = (),
                 executablePath: Optional[str] = None) -> None:
        if not 1_000 <= timeoutMs <= 120_000:
            raise LinkAuditError("timeoutMs must be in [1000, 120000]")
        self._allowPrivateNetwork = allowPrivateNetwork
        self._allowedOrigins = frozenset(o.lower().rstrip("/") for o in allowedOrigins)  # exact origins only (demo fixtures)
        self._timeoutMs = timeoutMs
        self._executablePath = executablePath or os.environ.get("SHIPPROOF_CHROMIUM_PATH") or None
        self._playwright = None
        self._browser = None

    def _permitted(self, url: str) -> bool:
        return _origin(url) in self._allowedOrigins or isPublicUrl(url, self._allowPrivateNetwork)

    def __enter__(self) -> LinkAuditor:
        try:
            from playwright.sync_api import Error as PlaywrightError, sync_playwright
        except ImportError as error:
            raise LinkAuditError("playwright is not installed: pip install -r requirements.txt") from error
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True, executable_path=self._executablePath)
        except PlaywrightError as error:
            self.__exit__(None, None, None)
            raise LinkAuditError(f"cannot launch Chromium (run 'python -m playwright install chromium'): {error}") from error
        return self

    def __exit__(self, *_: object) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def render(self, url: str) -> RenderedPage:
        """Loads a URL as a logged-out visitor. Raises UnsafeUrlError for non-public targets, LinkAuditError on load failure."""
        from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

        if self._browser is None:
            raise LinkAuditError("LinkAuditor used outside its context manager")
        if _origin(url) not in self._allowedOrigins:
            assertPublicUrl(url, self._allowPrivateNetwork)
        context = self._browser.new_context(locale="en-US", java_script_enabled=True, accept_downloads=False,
                                            service_workers="block")
        try:
            context.route("**/*", lambda route: route.continue_()
                          if self._permitted(route.request.url) or route.request.url.startswith(("data:", "blob:"))
                          else route.abort("blockedbyclient"))
            page = context.new_page()
            page.set_default_timeout(self._timeoutMs)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=self._timeoutMs)
            except PlaywrightError as error:
                raise LinkAuditError(f"navigation failed: {str(error).splitlines()[0]}") from error
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
            except PlaywrightTimeout:
                pass  # Best-effort settle for single-page apps; streaming sites never go idle. Content is read regardless.
            finalUrl = page.url
            if not self._permitted(finalUrl):
                raise UnsafeUrlError(f"redirected to non-public target {finalUrl[:200]}")
            fullText = page.inner_text("body") if page.locator("body").count() else ""
            hrefs = page.eval_on_selector_all("a[href]", "els => els.slice(0, 500).map(e => e.href)")
            links = tuple(dict.fromkeys(h for h in hrefs if isinstance(h, str) and h.startswith(("http://", "https://")) and len(h) <= 2000))[:MAX_PAGE_LINKS]
            hasPasswordField = page.locator("input[type=password]").count() > 0
        finally:
            context.close()

        text = fullText[:MAX_PAGE_TEXT_CHARS]
        reasons = detectAuthWall(finalUrl, text, hasPasswordField)
        return RenderedPage(
            requestedUrl=url, finalUrl=finalUrl, finalStatus=response.status if response is not None else None,
            text=text, textTruncated=len(fullText) > MAX_PAGE_TEXT_CHARS,
            contentSha256=hashlib.sha256(fullText.encode("utf-8")).hexdigest(),
            authWallDetected=bool(reasons), authWallReasons=reasons, unavailableReasons=detectUnavailable(text), links=links,
        )

    def audit(self, url: str, evidenceId: EntityId) -> EvidenceItem:
        """PASS only for a 2xx final response with no auth-wall signal. Failures are recorded, never raised."""
        try:
            page = self.render(url)
        except (UnsafeUrlError, LinkAuditError) as error:
            return EvidenceItem(evidenceId=evidenceId, url=url, method=EvidenceMethod.HEADLESS_BROWSER,
                                authWallDetected=False, status=CheckStatus.FAIL, checkedAt=utcNow(),
                                detail=f"{type(error).__name__}: {error}"[:500])
        ok = (page.finalStatus is not None and 200 <= page.finalStatus <= 299
              and not page.authWallDetected and not page.unavailableReasons)
        if ok:
            detail = "Viewable by a logged-out visitor."
        elif page.authWallDetected:
            detail = f"Auth wall: {', '.join(page.authWallReasons)}"
        elif page.unavailableReasons:
            detail = f"Content unavailable: {', '.join(page.unavailableReasons)}"
        else:
            detail = f"Final status {page.finalStatus}"
        return EvidenceItem(
            evidenceId=evidenceId, url=url, method=EvidenceMethod.HEADLESS_BROWSER, finalStatus=page.finalStatus,
            finalUrl=page.finalUrl if page.finalUrl.startswith(("http://", "https://")) else None,
            authWallDetected=page.authWallDetected, status=CheckStatus.PASS if ok else CheckStatus.FAIL,
            checkedAt=utcNow(), detail=detail[:500],
        )
