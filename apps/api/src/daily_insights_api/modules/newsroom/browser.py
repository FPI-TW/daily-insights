"""Article pages read through CloakBrowser (a Chromium build) over CDP.

The fetch stage turns to this only after the plain HTTP read was refused as a
bot (403 or a Cloudflare challenge) or returned text that is not the article,
which usually means the page renders its body with JavaScript.

The browser runs in its own container on an internal network; its only way out
is the egress proxy (``ingestion/egress_proxy.py``), which tunnels to public
addresses on 443 and nothing else. On this side every main-frame navigation,
redirects included, must pass the caller's ``guard`` (allowlist and
robots.txt), and images, media and fonts are never downloaded.

Playwright's ``page.route`` is not consulted for redirect hops, so requests are
paused through the CDP ``Fetch`` domain instead: a main-frame document is
checked at the request stage, and a redirect's ``Location`` at the response
stage, before the browser follows it.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Page,
    Playwright,
    Request,
    Response,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from daily_insights_api.core.config import Settings

# Returns why ``url`` may not be opened as the page, or None when it may.
NavigationGuard = Callable[[str], Awaitable[str | None]]

SKIPPED_RESOURCES = ("Image", "Media", "Font")
INTERCEPT_PATTERNS = [
    {"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"},
    {"urlPattern": "*", "resourceType": "Document", "requestStage": "Response"},
    *(
        {"urlPattern": "*", "resourceType": kind, "requestStage": "Request"}
        for kind in SKIPPED_RESOURCES
    ),
]
CONNECT_TIMEOUT_MS = 15_000
# Waiting for network idle is best effort: ad and analytics beacons can keep a
# news page busy indefinitely.
SETTLE_TIMEOUT_MS = 5_000
CHALLENGE_POLL_SECONDS = 1.0
# Cloudflare's interstitial defines this until it hands over to the real page.
CHALLENGE_PROBE = "() => typeof window._cf_chl_opt !== 'undefined'"
# The rendered DOM inlines megabytes of scripts and state (CNBC); none of it is
# text, so it is removed before the page is serialized. JSON-LD stays: it
# carries the paywall marker.
STRIP_NON_TEXT = """() => document.querySelectorAll(
  'script:not([type="application/ld+json"]), style, noscript, template, svg, iframe, link'
).forEach((node) => node.remove())"""


class RenderFailedError(Exception):
    """The browser produced no usable page; the plain HTTP outcome stands."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RenderedPage:
    url: str
    status: int
    content_type: str
    html: str


class PageRenderer(Protocol):
    async def render(self, url: str, guard: NavigationGuard) -> RenderedPage: ...

    async def aclose(self) -> None: ...


def _is_main_frame_navigation(request: Request) -> bool:
    try:
        return request.is_navigation_request() and request.frame.parent_frame is None
    except PlaywrightError:
        # Service-worker requests have no frame; they are blocked per context.
        return False


class CdpRenderer:
    """One CDP connection shared by the worker, one fresh context per article."""

    def __init__(self, cdp_url: str, *, concurrency: int, timeout_seconds: float) -> None:
        self._cdp_url = cdp_url
        self._timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def _connected(self) -> Browser:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            self._browser = None
            try:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    self._cdp_url, timeout=CONNECT_TIMEOUT_MS
                )
            except PlaywrightError as error:
                raise RenderFailedError("browser_unreachable") from error
            return self._browser

    async def render(self, url: str, guard: NavigationGuard) -> RenderedPage:
        async with self._slots:
            browser = await self._connected()
            try:
                context = await browser.new_context(service_workers="block", accept_downloads=False)
            except PlaywrightError as error:
                raise RenderFailedError("browser_unreachable") from error
            try:
                context.set_default_timeout(self._timeout_seconds * 1000)
                return await asyncio.wait_for(
                    _render(context, url, guard), timeout=self._timeout_seconds
                )
            except TimeoutError as error:
                raise RenderFailedError("browser_timeout") from error
            except PlaywrightError as error:
                raise RenderFailedError("browser_error") from error
            finally:
                with contextlib.suppress(PlaywrightError):
                    await context.close()

    async def aclose(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(PlaywrightError):
                await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


class _MainFrameGate:
    """Decides every request paused by CDP ``Fetch`` for one page."""

    def __init__(self, session: CDPSession, frame_id: str, guard: NavigationGuard) -> None:
        self._session = session
        self._frame_id = frame_id
        self._guard = guard
        self._pending: set[asyncio.Future[None]] = set()
        self.refusals: list[str] = []

    def on_paused(self, event: dict[str, Any]) -> None:
        task = asyncio.ensure_future(self._decide(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _decide(self, event: dict[str, Any]) -> None:
        request_id = event["requestId"]
        try:
            if await self._allows(event):
                await self._session.send("Fetch.continueRequest", {"requestId": request_id})
            else:
                await self._session.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                )
        except PlaywrightError:
            # The page closed before the decision arrived.
            pass

    async def _allows(self, event: dict[str, Any]) -> bool:
        if event["resourceType"] != "Document":
            return False
        if event.get("frameId") != self._frame_id:
            return True
        url = event["request"]["url"]
        if "responseStatusCode" in event or "responseErrorReason" in event:
            status = event.get("responseStatusCode", 0)
            location = next(
                (
                    header["value"]
                    for header in event.get("responseHeaders", [])
                    if header["name"].lower() == "location"
                ),
                None,
            )
            if not 300 <= status < 400 or location is None:
                return True
            url = urljoin(url, location)
        reason = await self._guard(url)
        if reason is not None:
            self.refusals.append(reason)
        return reason is None


async def _render(context: BrowserContext, url: str, guard: NavigationGuard) -> RenderedPage:
    page = await context.new_page()
    session = await context.new_cdp_session(page)
    tree = await session.send("Page.getFrameTree")
    gate = _MainFrameGate(session, tree["frameTree"]["frame"]["id"], guard)
    session.on("Fetch.requestPaused", gate.on_paused)
    await session.send("Fetch.enable", {"patterns": INTERCEPT_PATTERNS})
    documents: list[Response] = []

    def record(response: Response) -> None:
        if _is_main_frame_navigation(response.request):
            documents.append(response)

    page.on("response", record)
    try:
        await page.goto(url, wait_until="domcontentloaded")
    except PlaywrightError as error:
        reason = gate.refusals[0] if gate.refusals else "browser_navigation_failed"
        raise RenderFailedError(reason) from error
    await _settle(page)
    # The gate saw every hop; checking where the page ended up costs nothing
    # (the guard caches by URL) and does not depend on frame bookkeeping.
    final_refusal = await guard(page.url)
    if gate.refusals or final_refusal is not None:
        raise RenderFailedError(gate.refusals[0] if gate.refusals else str(final_refusal))
    if not documents:
        raise RenderFailedError("browser_no_response")
    final = documents[-1]
    await page.evaluate(STRIP_NON_TEXT)
    html = await page.content()
    content_type = await final.header_value("content-type") or ""
    return RenderedPage(page.url, final.status, content_type, html)


async def _settle(page: Page) -> None:
    """Let scripts fill the page and wait out a bot challenge (bounded by the caller)."""
    while True:
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
        try:
            challenged = await page.evaluate(CHALLENGE_PROBE)
        except PlaywrightError:
            # The challenge navigated away mid-check; look again once it lands.
            challenged = True
        if not challenged:
            return
        await asyncio.sleep(CHALLENGE_POLL_SECONDS)


def build_renderer(settings: Settings) -> PageRenderer | None:
    """The CloakBrowser renderer, or None when no browser service is configured."""
    if not settings.newsroom_browser_cdp_url:
        return None
    return CdpRenderer(
        settings.newsroom_browser_cdp_url,
        concurrency=settings.newsroom_browser_concurrency,
        timeout_seconds=settings.newsroom_browser_timeout_seconds,
    )
