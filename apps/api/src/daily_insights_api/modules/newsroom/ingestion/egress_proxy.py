"""The CONNECT proxy that is the browser container's only way out.

CloakBrowser sits on an internal Docker network with no route to the internet;
Chrome is started with ``--proxy-server`` pointing here. This proxy, which also
sits on the egress network, tunnels only ``CONNECT <hostname>:443`` to a host
that resolves solely to public addresses, and connects to the address it
validated, so neither the browser nor a DNS rebind can reach the app network,
the database or the cloud metadata endpoint.

Page subresources come from CDNs and challenge providers, so the article
allowlist is not applied here; the fetch stage enforces it on every main-frame
navigation (``newsroom/browser.py``).
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from daily_insights_api.modules.newsroom.ingestion.safe_http import (
    Resolver,
    UnsafeDestinationError,
    assert_public_hostname,
    normalize_hostname,
)

logger = logging.getLogger(__name__)

PORT = 3128
MAX_HEAD_BYTES = 8192
HEAD_TIMEOUT_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 10.0
IDLE_TIMEOUT_SECONDS = 120.0
MAX_TUNNELS = 128
CHUNK_BYTES = 65_536

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class TunnelRefusedError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def parse_connect(head: bytes) -> str:
    """The hostname of a ``CONNECT <hostname>:443`` request head."""
    parts = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise TunnelRefusedError(400, "Bad Request")
    method, target, _ = parts
    if method != "CONNECT":
        raise TunnelRefusedError(405, "Method Not Allowed")
    host, separator, port = target.rpartition(":")
    if not separator or not host or port != "443":
        raise TunnelRefusedError(403, "Forbidden")
    return normalize_hostname(host.strip("[]"))


async def _open_connection(
    host: str, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(host, port)


class EgressProxy:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        connector: Connector = _open_connection,
        max_tunnels: int = MAX_TUNNELS,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self._resolver = resolver
        self._connector = connector
        self._slots = asyncio.Semaphore(max_tunnels)
        self._idle_timeout = idle_timeout

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self._slots.locked():
                await _reply(writer, 503, "Service Unavailable")
                return
            async with self._slots:
                await self._tunnel(reader, writer)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_SECONDS)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            return
        try:
            host = parse_connect(head)
        except TunnelRefusedError as error:
            await _reply(writer, error.status, error.reason)
            return
        try:
            addresses = await assert_public_hostname(host, None, self._resolver)
        except UnsafeDestinationError:
            logger.info("newsroom.egress_refused", extra={"host": host})
            await _reply(writer, 403, "Forbidden")
            return
        except OSError:
            await _reply(writer, 502, "Bad Gateway")
            return
        upstream = await self._connect(addresses)
        if upstream is None:
            await _reply(writer, 502, "Bad Gateway")
            return
        upstream_reader, upstream_writer = upstream
        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._pipe(reader, writer, upstream_reader, upstream_writer)
        finally:
            upstream_writer.close()
            with contextlib.suppress(ConnectionError):
                await upstream_writer.wait_closed()

    async def _connect(
        self, addresses: list[str]
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        for address in dict.fromkeys(addresses):
            try:
                return await asyncio.wait_for(
                    self._connector(address, 443), CONNECT_TIMEOUT_SECONDS
                )
            except (OSError, TimeoutError):
                continue
        return None

    async def _pipe(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        pumps = {
            asyncio.ensure_future(self._pump(client_reader, upstream_writer)),
            asyncio.ensure_future(self._pump(upstream_reader, client_writer)),
        }
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)

    async def _pump(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError, TimeoutError):
            while chunk := await asyncio.wait_for(source.read(CHUNK_BYTES), self._idle_timeout):
                sink.write(chunk)
                await sink.drain()


async def _reply(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n\r\n".encode("latin-1"))
    with contextlib.suppress(ConnectionError):
        await writer.drain()


async def serve(host: str = "0.0.0.0", port: int = PORT) -> None:
    proxy = EgressProxy()
    server = await asyncio.start_server(proxy.handle, host, port, limit=MAX_HEAD_BYTES)
    async with server:
        await server.serve_forever()
