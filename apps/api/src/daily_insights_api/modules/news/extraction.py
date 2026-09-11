"""SSRF-safe, in-memory article extraction for allowlisted publishers."""

import asyncio
import ipaddress
import re
import socket
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpcore
import httpx
from httpcore._backends.base import SOCKET_OPTION
from httpx._config import create_ssl_context
from httpx._models import Response
from httpx._transports.base import AsyncBaseTransport
from httpx._transports.default import AsyncResponseStream, map_httpcore_exceptions
from httpx._types import AsyncByteStream

from daily_insights_api.modules.news.contracts import Candidate

MAX_BYTES = 1_500_000
MAX_ARTICLE_CHARS = 40_000

Resolver = Callable[[str, int], Awaitable[list[str]]]


@dataclass(frozen=True)
class FetchedCandidate:
    candidate: Candidate
    source_url: str
    body: str
    content_digest: str
    source_published_at: datetime | None = None


class _AllowlistedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve and connect to the same validated public IP, preventing DNS rebind."""

    def __init__(
        self,
        allowed: frozenset[str],
        delegate: httpcore.AsyncNetworkBackend | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.allowed = frozenset(host.lower().rstrip(".") for host in allowed)
        self.delegate = delegate or httpcore.AnyIOBackend()
        self.resolver = resolver or _resolve_addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        normalized = host.lower().rstrip(".")
        if normalized not in self.allowed or port != 443:
            raise httpcore.ConnectError("unsafe destination")
        try:
            addresses = await self.resolver(normalized, port)
        except OSError as error:
            raise httpcore.ConnectError("unable to resolve destination") from error
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise httpcore.ConnectError("destination is not exclusively public")
        last_error: Exception | None = None
        for address in dict.fromkeys(addresses):
            try:
                return await self.delegate.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except Exception as error:
                last_error = error
        raise httpcore.ConnectError("validated addresses could not connect") from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self.delegate.sleep(seconds)


async def _resolve_addresses(hostname: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(item[4][0] for item in infos))


class _SafeArticleTransport(AsyncBaseTransport):
    def __init__(self, allowed: frozenset[str]) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=create_ssl_context(verify=True, cert=None, trust_env=False),
            network_backend=_AllowlistedNetworkBackend(allowed),
            http2=False,
        )

    async def handle_async_request(self, request: httpx.Request) -> Response:
        from daily_insights_api.modules.news.recovery import check_dependency, current_workflow

        workflow = current_workflow()
        if workflow is not None:
            await workflow.check(workflow.stage)
            await check_dependency(workflow, f"source:{request.url.host}")
        assert isinstance(request.stream, AsyncByteStream)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with map_httpcore_exceptions():
            core_response = await self._pool.handle_async_request(core_request)
        assert isinstance(core_response.stream, AsyncIterable)
        return Response(
            status_code=core_response.status,
            headers=core_response.headers,
            stream=AsyncResponseStream(core_response.stream),
            extensions=core_response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def safe_article_client(allowed: frozenset[str], timeout_seconds: float = 25) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_SafeArticleTransport(allowed),
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        cookies=None,
        trust_env=False,
    )


def configured_hostnames(value: str) -> frozenset[str]:
    hosts = frozenset(part.strip().lower().rstrip(".") for part in value.split(",") if part.strip())
    if not hosts or any("." not in host or "/" in host or ":" in host for host in hosts):
        raise ValueError("news_allowed_hostnames must contain exact hostnames")
    return hosts


def allowed_hostname(hostname: str, allowed: frozenset[str]) -> bool:
    return hostname.lower().rstrip(".") in allowed


async def assert_public_hostname(hostname: str, allowed: frozenset[str]) -> None:
    if not allowed_hostname(hostname, allowed):
        raise ValueError("source host is not allowlisted")
    infos = await asyncio.get_running_loop().getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    addresses = {item[4][0] for item in infos}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("source host does not resolve solely to public IPs")


def validate_https_url(url: str, allowed: frozenset[str]) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ValueError("only absolute HTTPS URLs without credentials are allowed")
    if not allowed_hostname(parsed.hostname, allowed):
        raise ValueError("source host is not allowlisted")
    return url


class _ArticleTextExtractor(HTMLParser):
    _SKIP = frozenset(
        {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form"}
    )

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = self._article_depth = self._main_depth = 0
        self.article_parts: list[str] = []
        self.main_parts: list[str] = []
        self.fallback_parts: list[str] = []
        self.published_at: datetime | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag == "meta" and values.get("property", "").lower() == "article:published_time":
            try:
                published_at = datetime.fromisoformat(values["content"].replace("Z", "+00:00"))
                if published_at.tzinfo is not None and published_at.utcoffset() is not None:
                    self.published_at = published_at.astimezone(UTC)
            except (KeyError, ValueError):
                pass
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag == "article":
            self._article_depth += 1
        if tag == "main":
            self._main_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "article" and self._article_depth:
            self._article_depth -= 1
        if tag == "main" and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._article_depth:
            self.article_parts.append(data)
        if self._main_depth:
            self.main_parts.append(data)
        self.fallback_parts.append(data)

    def text(self) -> str:
        return re.sub(
            r"\s+", " ", " ".join(self.article_parts or self.main_parts or self.fallback_parts)
        ).strip()


async def robots_allowed(client: httpx.AsyncClient, url: str, allowed: frozenset[str]) -> bool:
    parsed = urlparse(validate_https_url(url, allowed))
    await assert_public_hostname(parsed.hostname or "", allowed)
    async with client.stream(
        "GET", f"https://{parsed.netloc}/robots.txt", follow_redirects=False
    ) as response:
        if response.is_redirect:
            return False
        if response.status_code in {404, 410}:
            return True
        # A temporary robots outage is not permission to crawl. Preserve HTTP
        # metadata (especially Retry-After) for the source recovery policy.
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > 200_000:
                return False
            chunks.append(chunk)
    parser = RobotFileParser()
    parser.parse(b"".join(chunks).decode("utf-8", errors="replace").splitlines())
    return parser.can_fetch("DailyInsightsNewsBot", url)


async def fetch_article(
    client: httpx.AsyncClient, url: str, allowed: frozenset[str]
) -> tuple[str, str, datetime | None]:
    current = validate_https_url(url, allowed)
    for _ in range(4):
        parsed = urlparse(current)
        await assert_public_hostname(parsed.hostname or "", allowed)
        if not await robots_allowed(client, current, allowed):
            raise ValueError("robots disallow extraction")
        async with client.stream(
            "GET",
            current,
            follow_redirects=False,
            headers={"User-Agent": "DailyInsightsNewsBot/1.0", "Accept": "text/html"},
        ) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("redirect without location")
                current = validate_https_url(urljoin(current, location), allowed)
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError("unsupported article content type")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("article response exceeds size limit")
                chunks.append(chunk)
        parser = _ArticleTextExtractor()
        parser.feed(b"".join(chunks).decode("utf-8", errors="replace"))
        text = parser.text()
        if len(text) < 200:
            raise ValueError("article extraction too short or paywalled")
        return current, text[:MAX_ARTICLE_CHARS], parser.published_at
    raise ValueError("too many redirects")


def _title_key(title: str) -> str:
    return " ".join(re.findall(r"[\w]+", title.lower()))[:240]


def _dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique: list[Candidate] = []
    for candidate in candidates:
        url, key = str(candidate.url), _title_key(candidate.headline)
        tokens = set(key.split())
        similar = any(
            len(tokens & set(previous.split())) / max(1, len(tokens | set(previous.split()))) >= 0.8
            for previous in seen_titles
        )
        if url in seen_urls or not key or key in seen_titles or similar:
            continue
        seen_urls.add(url)
        seen_titles.add(key)
        unique.append(candidate)
    return unique
