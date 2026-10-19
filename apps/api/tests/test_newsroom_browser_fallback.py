"""The fetch stage reads every page in CloakBrowser: outcomes, fallback, what it may open."""

from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
from playwright.async_api import CDPSession
from playwright.async_api import Error as PlaywrightError

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.browser import (
    CdpRenderer,
    NavigationGuard,
    RenderedPage,
    RenderFailedError,
    _MainFrameGate,
    build_renderer,
)
from daily_insights_api.modules.newsroom.ingestion.fetching import (
    ArticleUnavailableError,
    read_article,
)
from daily_insights_api.modules.newsroom.queue import RetryableStageError

HOST = "www.example-news.com"
ALLOWED = frozenset({HOST})
URL = f"https://{HOST}/markets/fed-holds-rates"
ARTICLE = (
    "<html><head><title>Fed holds rates steady</title></head><body><article><p>"
    + "The Federal Reserve holds rates steady while inflation cools across the economy. " * 6
    + "</p></article></body></html>"
)
PLAIN = ARTICLE.replace("The Federal Reserve", "The central bank")
PAYWALL = (
    '<html><head><script type="application/ld+json">{"isAccessibleForFree": false}</script>'
    "</head><body><article>The Federal Reserve holds rates steady.</article></body></html>"
)

Handler = Callable[[httpx.Request], httpx.Response]


async def public_resolver(host: str, port: int) -> list[str]:
    del host, port
    return ["93.184.216.34"]


class Site:
    """robots.txt plus one page, counting how often plain HTTP read the page."""

    def __init__(
        self,
        body: str = PLAIN,
        status: int = 200,
        robots: str = "User-agent: *\nAllow: /\n",
        robots_status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body, self.status, self.headers = body, status, headers or {}
        self.robots, self.robots_status = robots, robots_status
        self.page_reads = 0
        self.robots_reads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            self.robots_reads += 1
            return httpx.Response(self.robots_status, text=self.robots)
        self.page_reads += 1
        return httpx.Response(
            self.status, text=self.body, headers={"content-type": "text/html", **self.headers}
        )


def rendered(
    body: str = ARTICLE, status: int = 200, content_type: str = "text/html"
) -> RenderedPage:
    return RenderedPage(URL, status, content_type, body)


class FakeRenderer:
    """Opens ``url`` and then ``navigations`` through the guard, like the browser would."""

    def __init__(
        self,
        result: RenderedPage | RenderFailedError | None = None,
        navigations: tuple[str, ...] = (),
    ) -> None:
        self.result = result or rendered()
        self.navigations = navigations
        self.calls: list[str] = []
        self.verdicts: list[tuple[str, str | None]] = []

    async def render(self, url: str, guard: NavigationGuard) -> RenderedPage:
        self.calls.append(url)
        for target in (url, *self.navigations):
            reason = await guard(target)
            self.verdicts.append((target, reason))
            if reason is not None:
                raise RenderFailedError(reason)
        if isinstance(self.result, RenderFailedError):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        return None


async def read(handler: Handler, renderer: FakeRenderer | None) -> Any:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await read_article(client, URL, ALLOWED, resolver=public_resolver, renderer=renderer)


# --- every page goes through the browser ---------------------------------------


@pytest.mark.parametrize(
    "site", [Site(), Site(status=403), Site(status=503, headers={"cf-mitigated": "challenge"})]
)
async def test_every_page_is_read_in_the_browser(site: Site) -> None:
    renderer = FakeRenderer()

    fetched = await read(site, renderer)

    assert renderer.calls == [URL]
    assert fetched.via_browser
    assert fetched.page.text.startswith("The Federal Reserve holds rates steady")
    assert site.page_reads == 0


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (rendered(status=401), "access_denied"),
        (rendered(status=403), "access_denied"),
        (rendered(status=451), "access_denied"),
        (rendered(status=404), "not_found"),
        (rendered(status=410), "not_found"),
        (rendered(status=400), "http_400"),
        (rendered(content_type="application/pdf"), "not_html"),
        (rendered(body="x" * 1_600_000), "too_large"),
    ],
)
async def test_what_the_browser_got_is_the_final_answer(result: RenderedPage, reason: str) -> None:
    site = Site(status=403)

    with pytest.raises(ArticleUnavailableError) as raised:
        await read(site, FakeRenderer(result))

    assert raised.value.reason == reason
    # Only a refusal earns plain HTTP a try (refused here too); nothing else does.
    assert site.page_reads == (1 if reason == "access_denied" else 0)


async def test_a_page_refusing_the_browser_gets_one_plain_try() -> None:
    site = Site()

    fetched = await read(site, FakeRenderer(rendered(status=403)))

    assert not fetched.via_browser
    assert site.page_reads == 1
    with pytest.raises(ArticleUnavailableError, match="access_denied"):
        await read(Site(status=404), FakeRenderer(rendered(status=403)))


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_browser_rate_limits_and_outages_are_retried(status: int) -> None:
    with pytest.raises(RetryableStageError) as raised:
        await read(Site(), FakeRenderer(rendered(status=status)))

    assert raised.value.code == f"fetch_http_{status}"


async def test_a_browser_page_marked_paywalled_is_reported_as_such() -> None:
    fetched = await read(Site(), FakeRenderer(rendered(PAYWALL)))

    assert fetched.via_browser
    assert fetched.page.paywalled


# --- plain HTTP takes over when the browser fails ------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "browser_unreachable",
        "browser_timeout",
        "browser_error",
        "browser_navigation_failed",
        "browser_no_response",
    ],
)
async def test_plain_http_takes_over_when_the_browser_fails(reason: str) -> None:
    site = Site()

    fetched = await read(site, FakeRenderer(RenderFailedError(reason)))

    assert not fetched.via_browser
    assert fetched.page.text.startswith("The central bank holds rates steady")
    assert site.page_reads == 1


async def test_without_a_browser_plain_http_answers() -> None:
    assert not (await read(Site(), None)).via_browser
    with pytest.raises(ArticleUnavailableError, match="bot_challenge"):
        await read(Site(status=503, headers={"cf-mitigated": "challenge"}), None)


# --- what the browser may open -------------------------------------------------


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("https://evil.example/a", "host_not_allowed"),
        (f"http://{HOST}/markets/fed-holds-rates", "host_not_allowed"),
        (f"https://{HOST}/private/a", "robots_disallowed"),
        (f"https://{HOST}/markets/other", None),
    ],
)
async def test_browser_navigations_must_be_allowlisted_and_robots_allowed(
    target: str, reason: str | None
) -> None:
    renderer = FakeRenderer(navigations=(target,))
    site = Site(robots="User-agent: *\nDisallow: /private/\n")

    if reason is None:
        assert (await read(site, renderer)).via_browser
    else:
        # A refused navigation is final: plain HTTP must not read it instead.
        with pytest.raises(ArticleUnavailableError, match=reason):
            await read(site, renderer)

    assert renderer.verdicts[-1] == (target, reason)
    assert site.page_reads == 0


async def test_a_robots_outage_hands_the_page_to_plain_http_which_retries() -> None:
    site = Site(robots_status=503)
    renderer = FakeRenderer()

    with pytest.raises(RetryableStageError, match="fetch_http_503"):
        await read(site, renderer)

    assert renderer.verdicts == [(URL, "robots_unreachable")]
    assert site.robots_reads == 2
    assert site.page_reads == 0


# --- the CDP Fetch gate -------------------------------------------------------


class FakeSession:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict[str, Any] | None]] = []
        self.fail = fail

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.fail:
            raise PlaywrightError("Target closed")
        self.sent.append((method, params))
        return {}


def event(
    resource: str = "Document",
    frame: str = "main",
    url: str = URL,
    **response: Any,
) -> dict[str, Any]:
    return {
        "requestId": "r1",
        "resourceType": resource,
        "frameId": frame,
        "request": {"url": url},
        **response,
    }


class RecordingGuard:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def __call__(self, url: str) -> str | None:
        self.urls.append(url)
        return None if url.startswith(f"https://{HOST}/") else "host_not_allowed"


async def decide(paused: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    session = FakeSession()
    guard = RecordingGuard()
    gate = _MainFrameGate(cast(CDPSession, session), "main", guard)
    await gate._decide(paused)
    [(method, _)] = session.sent
    return method, guard.urls, gate.refusals


@pytest.mark.parametrize(
    ("paused", "method", "checked", "refusals"),
    [
        (event("Image"), "Fetch.failRequest", [], []),
        (event("Font"), "Fetch.failRequest", [], []),
        (event(), "Fetch.continueRequest", [URL], []),
        (
            event(url="https://evil.example/a"),
            "Fetch.failRequest",
            ["https://evil.example/a"],
            ["host_not_allowed"],
        ),
        # A redirect is judged by where it leads, before the browser follows it.
        (
            event(responseStatusCode=302, responseHeaders=[{"name": "Location", "value": "/b"}]),
            "Fetch.continueRequest",
            [f"https://{HOST}/b"],
            [],
        ),
        (
            event(
                responseStatusCode=301,
                responseHeaders=[{"name": "location", "value": "https://evil.example/a"}],
            ),
            "Fetch.failRequest",
            ["https://evil.example/a"],
            ["host_not_allowed"],
        ),
        (event(responseStatusCode=200, responseHeaders=[]), "Fetch.continueRequest", [], []),
        # Challenge iframes and ads load from elsewhere; only the page is guarded.
        (event(frame="child", url="https://evil.example/a"), "Fetch.continueRequest", [], []),
    ],
)
async def test_the_gate_guards_main_frame_documents_and_redirects(
    paused: dict[str, Any], method: str, checked: list[str], refusals: list[str]
) -> None:
    assert await decide(paused) == (method, checked, refusals)


async def test_the_gate_ignores_a_page_that_closed_first() -> None:
    gate = _MainFrameGate(cast(CDPSession, FakeSession(fail=True)), "main", RecordingGuard())

    await gate._decide(event())

    assert gate.refusals == []


# --- configuration ------------------------------------------------------------


def test_the_renderer_exists_only_when_a_browser_is_configured() -> None:
    assert build_renderer(Settings(_env_file=None, environment="test")) is None
    renderer = build_renderer(
        Settings(
            _env_file=None, environment="test", newsroom_browser_cdp_url="http://cloakbrowser:9222"
        )
    )
    assert isinstance(renderer, CdpRenderer)


@pytest.mark.parametrize(
    "url", ["ftp://cloakbrowser:9222", "http://user:secret@cloakbrowser:9222", "cloakbrowser"]
)
def test_the_worker_refuses_a_malformed_browser_url(url: str) -> None:
    with pytest.raises(ValueError, match="newsroom_browser_cdp_url"):
        Settings(
            _env_file=None,
            environment="test",
            runtime_role="newsroom-worker",
            newsroom_browser_cdp_url=url,
        )
