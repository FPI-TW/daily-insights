"""The browser's egress proxy tunnels to public HTTPS addresses and nothing else."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator

import pytest

from daily_insights_api.modules.newsroom.ingestion.egress_proxy import (
    MAX_HEAD_BYTES,
    EgressProxy,
)

PUBLIC = "93.184.216.34"


class Upstream:
    """Stands in for the internet: an echo server, reached whatever address is asked for."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.calls: list[tuple[str, int]] = []

    async def connect(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        self.calls.append((host, port))
        return await asyncio.open_connection("127.0.0.1", self.port)


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(1024):
        writer.write(b"echo:" + data)
        await writer.drain()
    writer.close()


@contextlib.asynccontextmanager
async def serving(
    addresses: list[str] | None = None, *, max_tunnels: int = 8, fail_connect: bool = False
) -> AsyncGenerator[tuple[int, Upstream]]:
    echo = await asyncio.start_server(_echo, "127.0.0.1", 0)
    upstream = Upstream(echo.sockets[0].getsockname()[1])

    async def resolver(host: str, port: int) -> list[str]:
        del host, port
        if addresses is None:
            raise OSError("name resolution failed")
        return addresses

    async def connector(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if fail_connect:
            upstream.calls.append((host, port))
            raise ConnectionRefusedError
        return await upstream.connect(host, port)

    proxy = EgressProxy(resolver=resolver, connector=connector, max_tunnels=max_tunnels)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0, limit=MAX_HEAD_BYTES)
    try:
        yield server.sockets[0].getsockname()[1], upstream
    finally:
        server.close()
        echo.close()


async def request(
    port: int, head: bytes
) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head)
    await writer.drain()
    status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    return status.split(b"\r\n", 1)[0], reader, writer


def connect(target: str) -> bytes:
    return f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()


async def status_of(port: int, head: bytes) -> bytes:
    line, _, writer = await request(port, head)
    writer.close()
    return line


async def test_tunnels_to_the_address_it_validated() -> None:
    async with serving([PUBLIC]) as (port, upstream):
        line, reader, writer = await request(port, connect("www.example-news.com:443"))
        writer.write(b"ping")
        await writer.drain()
        echoed = await asyncio.wait_for(reader.read(1024), 5)
        writer.close()

    assert line == b"HTTP/1.1 200 Connection Established"
    assert echoed == b"echo:ping"
    assert upstream.calls == [(PUBLIC, 443)]


@pytest.mark.parametrize(
    "addresses", [["10.0.0.5"], ["169.254.169.254"], [PUBLIC, "172.30.0.4"], []]
)
async def test_refuses_hosts_that_resolve_to_anything_private(addresses: list[str]) -> None:
    async with serving(addresses) as (port, upstream):
        line = await status_of(port, connect("rebind.example.com:443"))

    assert line == b"HTTP/1.1 403 Forbidden"
    assert upstream.calls == []


@pytest.mark.parametrize(
    ("head", "status"),
    [
        (connect("www.example-news.com:80"), b"403 Forbidden"),
        (connect("www.example-news.com"), b"403 Forbidden"),
        (connect("93.184.216.34:443"), b"403 Forbidden"),
        (connect("[2606:2800:220:1::]:443"), b"403 Forbidden"),
        (b"GET http://www.example-news.com/ HTTP/1.1\r\n\r\n", b"405 Method Not Allowed"),
        (b"hello\r\n\r\n", b"400 Bad Request"),
    ],
)
async def test_refuses_anything_but_connect_to_a_hostname_on_443(
    head: bytes, status: bytes
) -> None:
    async with serving([PUBLIC]) as (port, upstream):
        line = await status_of(port, head)

    assert line == b"HTTP/1.1 " + status
    assert upstream.calls == []


async def test_resolution_and_connection_failures_are_bad_gateway() -> None:
    async with serving(None) as (port, _):
        assert await status_of(port, connect("gone.example.com:443")) == b"HTTP/1.1 502 Bad Gateway"
    async with serving([PUBLIC], fail_connect=True) as (port, upstream):
        assert await status_of(port, connect("down.example.com:443")) == b"HTTP/1.1 502 Bad Gateway"
    assert upstream.calls == [(PUBLIC, 443)]


async def test_an_oversized_request_head_is_dropped() -> None:
    async with serving([PUBLIC]) as (port, upstream):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT " + b"a" * (MAX_HEAD_BYTES * 2))
        try:
            await writer.drain()
            dropped = await asyncio.wait_for(reader.read(), 5) == b""
        except ConnectionResetError:
            # The proxy closes with our bytes still unread, so the kernel may
            # answer with a reset instead of an orderly end of stream.
            dropped = True
        assert dropped
        writer.close()

    assert upstream.calls == []


async def test_turns_away_tunnels_beyond_its_limit() -> None:
    async with serving([PUBLIC], max_tunnels=1) as (port, _):
        line, _, first = await request(port, connect("one.example.com:443"))
        second = await status_of(port, connect("two.example.com:443"))
        first.close()

    assert line == b"HTTP/1.1 200 Connection Established"
    assert second == b"HTTP/1.1 503 Service Unavailable"
