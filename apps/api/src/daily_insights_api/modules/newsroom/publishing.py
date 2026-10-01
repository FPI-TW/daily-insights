"""Workstream ③: approval, 09:00 auto-publish, 12:00 late-fill close, edits (spec §6.3).

Owner: editions worktree. The admin API (workstream ④) calls only these
functions for anything that touches translation, analysis or publication;
each writes ``newsroom_edit_log``.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom.worker import Registration, Runtime


def register(runtime: Runtime) -> Registration:
    """Periodic tasks: auto-publish due drafts, close late fill at the deadline."""
    del runtime
    return Registration()


async def publish_edition(
    database: AsyncSession, edition_id: uuid.UUID, *, user_id: uuid.UUID | None
) -> None:
    """Approve a draft (``user_id``) or auto-publish it (``None``); queues English."""
    raise NotImplementedError


async def add_event_to_edition(
    database: AsyncSession, edition_id: uuid.UUID, event_id: uuid.UUID, *, user_id: uuid.UUID
) -> uuid.UUID:
    """Append a manual item; queues analysis or a single-market "why" as needed."""
    raise NotImplementedError


async def apply_event_edit(
    database: AsyncSession,
    event_id: uuid.UUID,
    *,
    headline: str | None,
    summary: str | None,
    related_symbols: list[dict[str, Any]] | None,
    user_id: uuid.UUID,
) -> None:
    """Save zh-hant edits, regenerate zh-hans, and mark English stale if published."""
    raise NotImplementedError


async def apply_why_edit(
    database: AsyncSession, item_id: uuid.UUID, why: str, *, user_id: uuid.UUID
) -> None:
    raise NotImplementedError


async def reanalyze_event(
    database: AsyncSession, event_id: uuid.UUID, *, user_id: uuid.UUID
) -> None:
    """Re-queue analysis for the event and "why" for every item placing it."""
    raise NotImplementedError
