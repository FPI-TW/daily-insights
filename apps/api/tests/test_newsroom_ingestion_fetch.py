"""SSRF-safe transport, page fetching outcomes, body quality, and the purge clock."""

import ssl
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpcore
import httpx
import pytest
from httpcore._backends.base import SOCKET_OPTION
from httpcore._models import Origin
from sqlalchemy.ext.asyncio import async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom import queue
from daily_insights_api.modules.newsroom.ingestion import register
from daily_insights_api.modules.newsroom.ingestion.extract import extract_page
from daily_insights_api.modules.newsroom.ingestion.fetching import (
    ArticleUnavailableError,
    fetch_page,
)
from daily_insights_api.modules.newsroom.ingestion.polling import sources_admin_url
from daily_insights_api.modules.newsroom.ingestion.purge import purge_cutoff
from daily_insights_api.modules.newsroom.ingestion.quality import (
    MIN_BODY_CHARS,
    assess,
    navigation_ratio,
    title_overlap,
)
from daily_insights_api.modules.newsroom.ingestion.safe_http import (
    _AllowlistedNetworkBackend,
    _SafeTransport,
    parse_hostnames,
    safe_client,
    validate_https_url,
)
from daily_insights_api.modules.newsroom.notifier import LogNotifier
from daily_insights_api.modules.newsroom.queue import RetryableStageError
from daily_insights_api.modules.newsroom.worker import Runtime, _UnconfiguredModel

HOST = "www.example-news.com"
ALLOWED = frozenset({HOST})
URL = f"https://{HOST}/markets/fed-holds-rates"
TITLE = "Fed holds rates steady as inflation cools"
ARTICLE = (
    "<html><head><title>Ignored</title>"
    '<meta property="og:title" content="Fed holds rates steady">'
    '<meta property="article:published_time" content="2026-09-02T02:30:00Z"></head>'
    "<body><nav>Home Markets Subscribe</nav><article><p>"
    + "The Federal Reserve holds rates steady while inflation cools across the economy. " * 6
    + "</p></article><footer>All rights reserved</footer></body></html>"
)


async def public_resolver(host: str, port: int) -> list[str]:
    del host, port
    return ["93.184.216.34"]


async def private_resolver(host: str, port: int) -> list[str]:
    del host, port
    return ["10.0.0.5"]


Handler = Callable[[httpx.Request], httpx.Response]


def site(page: Handler, robots: str = "User-agent: *\nAllow: /\n") -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return page(request)

    return handler


def html(body: str = ARTICLE, status: int = 200) -> Handler:
    return lambda _: httpx.Response(status, text=body, headers={"content-type": "text/html"})


async def fetch(handler: Handler, *, resolver: Any = public_resolver) -> Any:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await fetch_page(client, URL, ALLOWED, resolver=resolver)


# --- the DNS-pinned transport (moved from test_news_dns_backend.py) ------------


class RecordingStream(httpcore.AsyncNetworkStream):
    def __init__(self) -> None:
        self.sni: str | None = None

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        del max_bytes, timeout
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        del buffer, timeout
        raise httpcore.WriteError("stop after TLS")

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        del ssl_context, timeout
        self.sni = server_hostname
        return self

    def get_extra_info(self, info: str) -> Any:
        del info
        return None


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.stream = RecordingStream()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del timeout, local_address, socket_options
        self.calls.append((host, port))
        return self.stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise AssertionError("unix sockets must not be used")

    async def sleep(self, seconds: float) -> None:
        del seconds


async def test_backend_connects_to_the_verified_address_and_keeps_sni() -> None:
    delegate = RecordingBackend()
    connection = httpcore.AsyncHTTPConnection(
        origin=Origin(b"https", HOST.encode(), 443),
        network_backend=_AllowlistedNetworkBackend(
            ALLOWED, delegate=delegate, resolver=public_resolver
        ),
    )

    with pytest.raises(httpcore.RemoteProtocolError):
        await connection.handle_async_request(
            httpcore.Request("GET", URL, headers=[(b"host", HOST.encode())])
        )

    assert delegate.calls == [("93.184.216.34", 443)]
    assert delegate.stream.sni == HOST


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["8.8.8.8", "::1"]])
async def test_backend_rejects_private_or_mixed_dns_before_connect(addresses: list[str]) -> None:
    async def resolver(_: str, __: int) -> list[str]:
        return addresses

    delegate = RecordingBackend()
    backend = _AllowlistedNetworkBackend(ALLOWED, delegate=delegate, resolver=resolver)

    with pytest.raises(httpcore.ConnectError, match="exclusively public"):
        await backend.connect_tcp(HOST, 443)
    assert delegate.calls == []


async def test_backend_rejects_non443_and_unlisted_hosts_before_resolving() -> None:
    async def resolver(_: str, __: int) -> list[str]:
        raise AssertionError("unsafe destinations must not resolve")

    backend = _AllowlistedNetworkBackend(ALLOWED, delegate=RecordingBackend(), resolver=resolver)

    with pytest.raises(httpcore.ConnectError, match="unsafe destination"):
        await backend.connect_tcp(HOST, 8443)
    with pytest.raises(httpcore.ConnectError, match="unsafe destination"):
        await backend.connect_tcp("evil.example", 443)


async def test_safe_client_uses_the_pinned_transport() -> None:
    async with safe_client(ALLOWED, 5) as client:
        assert isinstance(client._transport, _SafeTransport)


def test_url_validation_and_hostname_settings() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        validate_https_url(f"https://{HOST}:8443/a", ALLOWED)
    with pytest.raises(ValueError, match="HTTPS"):
        validate_https_url(f"http://{HOST}/a", ALLOWED)
    with pytest.raises(ValueError, match="allowlisted"):
        validate_https_url("https://evil.example/a", ALLOWED)
    assert validate_https_url("https://evil.example/a", None) == "https://evil.example/a"
    assert parse_hostnames(" A.example.com., bad, x/y.com , b.example.org") == {
        "a.example.com",
        "b.example.org",
    }


# --- fetch outcomes -----------------------------------------------------------


async def test_fetch_extracts_the_article_text_title_and_time() -> None:
    fetched = await fetch(site(html()))

    assert fetched.url == URL
    assert fetched.page.text.startswith("The Federal Reserve holds rates steady")
    assert "Subscribe" not in fetched.page.text and "rights" not in fetched.page.text
    assert fetched.page.title == "Fed holds rates steady"
    assert fetched.page.published_at == datetime(2026, 9, 2, 2, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (site(html(), robots="User-agent: *\nDisallow: /markets/\n"), "robots_disallowed"),
        (
            site(
                lambda _: httpx.Response(
                    200, content=b"%PDF", headers={"content-type": "application/pdf"}
                )
            ),
            "not_html",
        ),
        (site(html(status=402)), "access_denied"),
        (site(html(status=403)), "access_denied"),
        (site(html(status=404)), "not_found"),
        (site(html(status=400)), "http_400"),
        (
            site(lambda _: httpx.Response(302, headers={"location": "https://evil.example/a"})),
            "redirect_not_allowed",
        ),
        (
            site(lambda _: httpx.Response(302, headers={"location": "/markets/fed-holds-rates"})),
            "too_many_redirects",
        ),
        (site(html(body="x" * 1_600_000)), "too_large"),
    ],
)
async def test_fetch_final_failures_are_unavailable(handler: Handler, reason: str) -> None:
    with pytest.raises(ArticleUnavailableError) as raised:
        await fetch(handler)

    assert raised.value.reason == reason


async def test_fetch_refuses_hosts_off_the_allowlist_or_resolving_privately() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(site(html()))) as client:
        with pytest.raises(ArticleUnavailableError, match="host_not_allowed"):
            await fetch_page(client, "https://evil.example/a", ALLOWED, resolver=public_resolver)
    with pytest.raises(ArticleUnavailableError, match="host_not_public"):
        await fetch(site(html()), resolver=private_resolver)


async def test_robots_client_errors_are_a_refusal_not_permission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403)
        return html()(request)

    with pytest.raises(ArticleUnavailableError, match="robots_unavailable"):
        await fetch(handler)


async def test_missing_robots_file_allows_fetching() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return html()(request)

    fetched = await fetch(handler)

    assert fetched.page.text


@pytest.mark.parametrize(
    ("handler", "code", "retry_after"),
    [
        (site(html(status=503)), "fetch_http_503", None),
        (
            site(lambda _: httpx.Response(429, headers={"retry-after": "120"})),
            "fetch_http_429",
            timedelta(seconds=120),
        ),
        (lambda _: httpx.Response(500), "fetch_http_500", None),
    ],
)
async def test_transient_http_failures_are_retryable(
    handler: Handler, code: str, retry_after: timedelta | None
) -> None:
    with pytest.raises(RetryableStageError) as raised:
        await fetch(handler)

    assert raised.value.code == code
    assert raised.value.retry_after == retry_after


async def test_network_failures_are_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async def no_dns(_: str, __: int) -> list[str]:
        raise OSError("name resolution failed")

    with pytest.raises(RetryableStageError, match="fetch_timeout"):
        await fetch(timeout)
    with pytest.raises(RetryableStageError, match="fetch_unreachable"):
        await fetch(refused)
    with pytest.raises(RetryableStageError, match="fetch_unreachable"):
        await fetch(site(html()), resolver=no_dns)


# --- extraction and quality ---------------------------------------------------


def test_paywall_marker_comes_from_json_ld() -> None:
    page = extract_page(
        '<script type="application/ld+json">'
        '{"@graph": [{"@type": "NewsArticle", "isAccessibleForFree": "False"}]}'
        "</script><article>Subscribe to read</article>"
    )

    assert page.paywalled is True
    assert page.text == "Subscribe to read"
    assert extract_page("<article>free</article>").paywalled is False


def test_quality_accepts_a_real_article() -> None:
    body = "The Federal Reserve holds rates steady while inflation cools. " * 5

    assert assess(TITLE, body).ok


def test_quality_rejects_short_text() -> None:
    verdict = assess(TITLE, "Fed holds rates. " * 5)

    assert len("Fed holds rates. " * 5) < MIN_BODY_CHARS
    assert (verdict.ok, verdict.reason) == (False, "too_short")


def test_quality_rejects_navigation_heavy_text() -> None:
    chrome = "Subscribe Sign in Newsletter Cookie Privacy policy Fed holds rates steady. " * 6

    assert navigation_ratio(chrome) > 0.1
    assert assess(TITLE, chrome).reason == "navigation_heavy"


def test_quality_rejects_text_unrelated_to_the_title() -> None:
    other = "Apple unveils a new phone with a larger camera and a faster chip for buyers. " * 4

    assert title_overlap(TITLE, other) == 0
    assert assess(TITLE, other).reason == "unrelated_to_title"


def test_quality_title_overlap_handles_chinese_and_short_titles() -> None:
    body = "台積電今日公布第二季財報。營收創新高。並上修全年展望。法人預期先進製程需求強勁。" * 5

    overlap = title_overlap("台積電第二季營收創新高", body)
    assert overlap is not None and overlap >= 0.8
    assert title_overlap("Fed", body) is None
    assert assess("Fed", body).ok


# --- purge clock --------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "cutoff"),
    [
        # 2026-10-02 02:59 Taipei: the boundary is still 2026-10-01 03:00.
        (datetime(2026, 10, 1, 18, 59, tzinfo=UTC), datetime(2026, 8, 31, 19, 0, tzinfo=UTC)),
        # 2026-10-02 03:00 Taipei moves it forward one day.
        (datetime(2026, 10, 1, 19, 0, tzinfo=UTC), datetime(2026, 9, 1, 19, 0, tzinfo=UTC)),
    ],
)
def test_purge_cutoff_moves_once_a_day_at_three_taipei(now: datetime, cutoff: datetime) -> None:
    assert purge_cutoff(now) == cutoff


# --- registration -------------------------------------------------------------


def test_register_binds_fetch_and_the_periodic_tasks() -> None:
    runtime = Runtime(
        settings=Settings(_env_file=None, environment="test"),
        session_factory=async_sessionmaker(),
        llm=_UnconfiguredModel("unused"),
        embedder=_UnconfiguredModel("unused"),
        notifier=LogNotifier(),
    )

    registration = register(runtime)

    [binding] = registration.stages
    assert binding.stage is queue.FETCH
    assert binding.concurrency == 4
    assert {task.name: task.interval for task in registration.periodic} == {
        "newsroom_poll_sources": timedelta(minutes=1),
        "newsroom_purge_bodies": timedelta(minutes=10),
    }


@pytest.mark.parametrize(
    "base_url", ["https://insights.example.com", "https://insights.example.com/"]
)
def test_unhealthy_notice_links_to_the_sources_admin_page(base_url: str) -> None:
    assert sources_admin_url(base_url) == "https://insights.example.com/admin/newsroom/sources"
