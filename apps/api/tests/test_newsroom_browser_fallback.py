"""The CloakBrowser fallback of the fetch stage: when it is used, and what it may open."""

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

HOST = "www.example-news.com"
ALLOWED = frozenset({HOST})
URL = f"https://{HOST}/markets/fed-holds-rates"
TITLE = "Fed holds rates steady as inflation cools"
ARTICLE = (
    "<html><head><title>Fed holds rates steady</title></head><body><article><p>"
    + "The Federal Reserve holds rates steady while inflation cools across the economy. " * 6
    + "</p></article></body></html>"
)
SHELL = "<html><body><main><div id='app'>Loading</div></main></body></html>"
PAYWALL = (
    '<html><head><script type="application/ld+json">{"isAccessibleForFree": false}</script>'
    "</head><body><article>The Federal Reserve holds rates steady.</article></body></html>"
)

Handler = Callable[[httpx.Request], httpx.Response]


async def public_resolver(host: str, port: int) -> list[str]:
    del host, port
    return ["93.184.216.34"]


def site(page: Handler, robots: str = "User-agent: *\nAllow: /\n") -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return page(request)

    return handler


def html(body: str = ARTICLE, status: int = 200, **headers: str) -> Handler:
    return lambda _: httpx.Response(
        status, text=body, headers={"content-type": "text/html", **headers}
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
        return await read_article(
            client, URL, ALLOWED, lambda _: TITLE, resolver=public_resolver, renderer=renderer
        )


# --- when the browser is used -------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        site(html(status=403)),
        site(html(status=403, **{"cf-mitigated": "challenge"})),
        site(html(status=503, **{"cf-mitigated": "challenge"})),
    ],
)
async def test_a_bot_refusal_is_read_again_in_the_browser(handler: Handler) -> None:
    renderer = FakeRenderer()

    fetched = await read(handler, renderer)

    assert renderer.calls == [URL]
    assert fetched.via_browser
    assert fetched.page.text.startswith("The Federal Reserve holds rates steady")


async def test_a_cloudflare_challenge_is_final_without_a_browser() -> None:
    with pytest.raises(ArticleUnavailableError) as raised:
        await read(site(html(status=503, **{"cf-mitigated": "challenge"})), None)

    assert raised.value.reason == "bot_challenge"
    assert raised.value.refused_as_bot


async def test_text_that_is_not_the_article_is_read_again_in_the_browser() -> None:
    renderer = FakeRenderer()

    fetched = await read(site(html(SHELL)), renderer)

    assert renderer.calls == [URL]
    assert fetched.via_browser


@pytest.mark.parametrize("body", [ARTICLE, PAYWALL])
async def test_an_article_or_a_paywall_never_reaches_the_browser(body: str) -> None:
    renderer = FakeRenderer()

    fetched = await read(site(html(body)), renderer)

    assert renderer.calls == []
    assert not fetched.via_browser


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (site(html(), robots="User-agent: *\nDisallow: /markets/\n"), "robots_disallowed"),
        (site(html(status=401)), "access_denied"),
        (site(html(status=402)), "access_denied"),
        (site(html(status=451)), "access_denied"),
        (site(html(status=404)), "not_found"),
        (
            site(lambda _: httpx.Response(302, headers={"location": "https://evil.example/a"})),
            "redirect_not_allowed",
        ),
    ],
)
async def test_final_answers_never_reach_the_browser(handler: Handler, reason: str) -> None:
    renderer = FakeRenderer()

    with pytest.raises(ArticleUnavailableError) as raised:
        await read(handler, renderer)

    assert raised.value.reason == reason
    assert renderer.calls == []


@pytest.mark.parametrize(
    "result",
    [
        RenderFailedError("browser_unreachable"),
        RenderFailedError("browser_timeout"),
        rendered(status=403),
        rendered(content_type="application/pdf"),
        rendered(body="x" * 1_600_000),
    ],
)
async def test_the_plain_outcome_stands_when_the_browser_has_nothing_better(
    result: RenderedPage | RenderFailedError,
) -> None:
    with pytest.raises(ArticleUnavailableError, match="access_denied"):
        await read(site(html(status=403)), FakeRenderer(result))

    fetched = await read(site(html(SHELL)), FakeRenderer(result))

    assert not fetched.via_browser
    assert fetched.page.text == "Loading"


async def test_a_browser_page_marked_paywalled_is_reported_as_such() -> None:
    fetched = await read(site(html(status=403)), FakeRenderer(rendered(PAYWALL)))

    assert fetched.via_browser
    assert fetched.page.paywalled


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
    handler = site(html(status=403), robots="User-agent: *\nDisallow: /private/\n")

    if reason is None:
        assert (await read(handler, renderer)).via_browser
    else:
        with pytest.raises(ArticleUnavailableError, match="access_denied"):
            await read(handler, renderer)

    assert renderer.verdicts[-1] == (target, reason)


async def test_robots_outages_refuse_the_navigation_and_verdicts_are_cached() -> None:
    robots_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal robots_requests
        if request.url.path == "/robots.txt":
            robots_requests += 1
            # The plain read sees the rules; the browser's checks meet an outage.
            return httpx.Response(200 if robots_requests == 1 else 503, text="")
        return html(status=403)(request)

    renderer = FakeRenderer(navigations=(URL,))

    with pytest.raises(ArticleUnavailableError, match="access_denied"):
        await read(handler, renderer)

    assert renderer.verdicts == [(URL, "robots_unreachable")]
    assert robots_requests == 2


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
