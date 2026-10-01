"""Workstream ①: admin-facing source and manual-article operations (spec §6.1).

Owner: ingestion worktree. The admin API (workstream ④) calls only these
functions; each writes ``newsroom_edit_log`` via ``editlog.record_edit``.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class SourceInput:
    key: str
    name: str
    kind: str
    url: str | None
    hostname: str
    markets: tuple[str, ...]
    trust_tier: int = 2
    weight: float = 1.0
    poll_interval_minutes: int = 30
    enabled: bool = True
    link_pattern: str | None = None
    language_filter: tuple[str, ...] | None = None
    full_text_in_feed: bool = False


async def create_source(
    database: AsyncSession, data: SourceInput, *, user_id: uuid.UUID
) -> uuid.UUID:
    raise NotImplementedError


async def update_source(
    database: AsyncSession,
    source_id: uuid.UUID,
    changes: dict[str, object],
    *,
    user_id: uuid.UUID,
) -> None:
    """Partial update of any ``SourceInput`` field; re-enabling resets ``next_poll_at``."""
    raise NotImplementedError


async def submit_manual_url(
    database: AsyncSession, url: str, *, edition_date: date, user_id: uuid.UUID
) -> uuid.UUID:
    """Create a ``manual``-source article queued for fetch → embed → triage."""
    raise NotImplementedError


async def set_manual_body(
    database: AsyncSession, article_id: uuid.UUID, body: str, *, user_id: uuid.UUID
) -> None:
    """Store a pasted body (``body_source = manual``) and unblock a ``needs_body`` event."""
    raise NotImplementedError
