"""Seams for the network and the clock, so tests never reach the internet."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from daily_insights_api.modules.newsroom.browser import PageRenderer
from daily_insights_api.modules.newsroom.ingestion.safe_http import Resolver, safe_client

ClientFactory = Callable[[frozenset[str], float], httpx.AsyncClient]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IngestionDeps:
    # Builds the SSRF-safe client for one allowlist; tests swap in a MockTransport.
    client_factory: ClientFactory = safe_client
    # DNS for the public-address pre-check; None uses the system resolver.
    resolver: Resolver | None = None
    clock: Callable[[], datetime] = field(default=_utcnow)
    # Reads a page in CloakBrowser when the plain read was refused or came back
    # without the article; None keeps the plain HTTP outcome.
    renderer: PageRenderer | None = None
