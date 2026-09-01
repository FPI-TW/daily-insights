import ssl
from collections.abc import Iterable
from typing import Any

import httpcore
import pytest
from httpcore._backends.base import SOCKET_OPTION
from httpcore._models import Origin

from daily_insights_api.modules.news.sources import (
    _AllowlistedNetworkBackend,
    _SafeArticleTransport,
    safe_article_client,
)

ALLOWED = frozenset({"www.reuters.com"})


class RecordingStream(httpcore.AsyncNetworkStream):
    def __init__(self) -> None:
        self.sni: str | None = None

    async def read(
        self,
        max_bytes: int,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> bytes:
        del max_bytes, timeout
        return b""

    async def write(
        self,
        buffer: bytes,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> None:
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


async def test_network_backend_connects_to_verified_literal_not_rebound_hostname() -> None:
    """The delegated AnyIO backend only receives the verified address, never the hostname."""

    async def resolver(_: str, __: int) -> list[str]:
        # A delegate resolving ``www.reuters.com`` again would receive a private rebinding answer.
        return ["8.8.8.8"]

    delegate = RecordingBackend()
    backend = _AllowlistedNetworkBackend(ALLOWED, delegate=delegate, resolver=resolver)

    await backend.connect_tcp("www.reuters.com", 443)

    assert delegate.calls == [("8.8.8.8", 443)]


async def test_httpcore_keeps_original_hostname_for_tls_sni_after_ip_connect() -> None:
    async def resolver(_: str, __: int) -> list[str]:
        return ["8.8.8.8"]

    delegate = RecordingBackend()
    connection = httpcore.AsyncHTTPConnection(
        origin=Origin(b"https", b"www.reuters.com", 443),
        network_backend=_AllowlistedNetworkBackend(ALLOWED, delegate=delegate, resolver=resolver),
    )

    with pytest.raises(httpcore.RemoteProtocolError, match="Server disconnected"):
        await connection.handle_async_request(
            httpcore.Request(
                "GET",
                "https://www.reuters.com/article",
                headers=[(b"host", b"www.reuters.com")],
            )
        )

    assert delegate.calls == [("8.8.8.8", 443)]
    assert delegate.stream.sni == "www.reuters.com"


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["8.8.8.8", "::1"]])
async def test_network_backend_rejects_private_or_mixed_dns_before_connect(
    addresses: list[str],
) -> None:
    async def resolver(_: str, __: int) -> list[str]:
        return addresses

    delegate = RecordingBackend()
    backend = _AllowlistedNetworkBackend(ALLOWED, delegate=delegate, resolver=resolver)

    with pytest.raises(httpcore.ConnectError, match="exclusively public"):
        await backend.connect_tcp("www.reuters.com", 443)

    assert delegate.calls == []


async def test_network_backend_rejects_non443_and_non_allowlisted_before_resolve() -> None:
    async def resolver(_: str, __: int) -> list[str]:
        raise AssertionError("unsafe destinations must not resolve")

    delegate = RecordingBackend()
    backend = _AllowlistedNetworkBackend(ALLOWED, delegate=delegate, resolver=resolver)

    with pytest.raises(httpcore.ConnectError, match="unsafe destination"):
        await backend.connect_tcp("www.reuters.com", 8443)
    with pytest.raises(httpcore.ConnectError, match="unsafe destination"):
        await backend.connect_tcp("evil.example", 443)

    assert delegate.calls == []


async def test_safe_article_client_applies_the_dns_bound_transport_to_articles_and_robots() -> None:
    async with safe_article_client(ALLOWED) as client:
        assert isinstance(client._transport, _SafeArticleTransport)  # pyright: ignore[reportPrivateUsage]
