"""SSRF-safe HTTP for feeds and article pages (moved from ``news/extraction.py``).

Every request goes through a transport that only connects to allowlisted
hostnames on port 443, resolves them once, and connects to the validated public
address itself, so a DNS rebind between check and connect cannot reach a
private network. Redirects are never followed automatically, every body is
byte-capped, and robots.txt is honoured before any page is read.
"""

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpcore
import httpx
from httpcore._backends.base import SOCKET_OPTION
from httpx._config import create_ssl_context
from httpx._models import Response
from httpx._transports.base import AsyncBaseTransport
from httpx._transports.default import AsyncResponseStream, map_httpcore_exceptions
from httpx._types import AsyncByteStream

USER_AGENT = "DailyInsightsNewsBot/1.0"
ROBOTS_AGENT = "DailyInsightsNewsBot"
MAX_ROBOTS_BYTES = 200_000

Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeDestinationError(ValueError):
    """The URL or its host failed SSRF validation; retrying cannot help."""


class ResponseTooLargeError(ValueError):
    """The body exceeded the byte cap for its kind of request."""


def normalize_hostname(hostname: str) -> str:
    return hostname.strip().lower().rstrip(".")


def parse_hostnames(value: str) -> frozenset[str]:
    """Comma-separated exact hostnames; malformed entries are ignored."""
    hosts = (normalize_hostname(part) for part in value.split(","))
    return frozenset(
        host for host in hosts if host and "." in host and "/" not in host and ":" not in host
    )


class _AllowlistedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve and connect to the same validated public IP, preventing DNS rebind."""

    def __init__(
        self,
        allowed: frozenset[str],
        delegate: httpcore.AsyncNetworkBackend | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.allowed = frozenset(normalize_hostname(host) for host in allowed)
        self.delegate: httpcore.AsyncNetworkBackend = delegate or httpcore.AnyIOBackend()
        self.resolver = resolver or resolve_addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        normalized = normalize_hostname(host)
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


async def resolve_addresses(hostname: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(item[4][0]) for item in infos))


class _SafeTransport(AsyncBaseTransport):
    def __init__(self, allowed: frozenset[str]) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=create_ssl_context(verify=True, cert=None, trust_env=False),
            network_backend=_AllowlistedNetworkBackend(allowed),
            http2=False,
        )

    async def handle_async_request(self, request: httpx.Request) -> Response:
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


def safe_client(allowed: frozenset[str], timeout_seconds: float) -> httpx.AsyncClient:
    """A client that can only reach ``allowed`` hosts over public HTTPS."""
    return httpx.AsyncClient(
        transport=_SafeTransport(allowed),
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        cookies=None,
        trust_env=False,
        headers={"User-Agent": USER_AGENT},
    )


def validate_https_url(url: str, allowed: frozenset[str] | None) -> str:
    """Absolute HTTPS on 443 without credentials; ``allowed=None`` skips the allowlist."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise UnsafeDestinationError("only absolute HTTPS URLs without credentials are allowed")
    if allowed is not None and normalize_hostname(parsed.hostname) not in allowed:
        raise UnsafeDestinationError("source host is not allowlisted")
    return url


async def assert_public_hostname(
    hostname: str, allowed: frozenset[str] | None, resolver: Resolver | None = None
) -> None:
    """The host is allowlisted (unless ``allowed`` is None) and resolves only to public IPs.

    Resolution failures are ``OSError`` (transient); a private answer is unsafe.
    """
    normalized = normalize_hostname(hostname)
    if allowed is not None and normalized not in allowed:
        raise UnsafeDestinationError("source host is not allowlisted")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise UnsafeDestinationError("IP literal hosts are not allowed")
    addresses = await (resolver or resolve_addresses)(normalized, 443)
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeDestinationError("source host does not resolve solely to public IPs")


class RobotsUnavailableError(ValueError):
    """robots.txt answered a client error; that is not permission to crawl."""


async def robots_allowed(
    client: httpx.AsyncClient,
    url: str,
    allowed: frozenset[str],
    *,
    resolver: Resolver | None = None,
) -> bool:
    """Whether robots.txt lets the bot read ``url``.

    404/410 mean no rules. A 4xx is treated as a refusal and a 5xx/429 raises
    ``HTTPStatusError`` so the caller can retry: a robots outage is never
    permission to crawl.
    """
    parsed = urlparse(validate_https_url(url, allowed))
    await assert_public_hostname(parsed.hostname or "", allowed, resolver)
    async with client.stream(
        "GET", f"https://{parsed.netloc}/robots.txt", follow_redirects=False
    ) as response:
        if response.is_redirect:
            return False
        if response.status_code in {404, 410}:
            return True
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise RobotsUnavailableError(f"robots.txt answered {response.status_code}")
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_ROBOTS_BYTES:
                return False
            chunks.append(chunk)
    parser = RobotFileParser()
    parser.parse(b"".join(chunks).decode("utf-8", errors="replace").splitlines())
    return parser.can_fetch(ROBOTS_AGENT, url)


async def read_capped(response: httpx.Response, limit: int, *, what: str = "response") -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise ResponseTooLargeError(f"{what} exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)
