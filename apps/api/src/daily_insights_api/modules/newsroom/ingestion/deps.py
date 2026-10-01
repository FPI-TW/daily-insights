"""Seams for the network and the clock, so tests never reach the internet."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

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
