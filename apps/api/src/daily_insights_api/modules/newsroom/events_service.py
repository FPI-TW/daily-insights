"""Workstream ②: admin event merge and split (spec §6.2, D10).

Owner: triage worktree. Both operations re-queue analysis for affected events
that sit in an edition, and write ``newsroom_edit_log``.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession


async def merge_events(
    database: AsyncSession,
    *,
    target_id: uuid.UUID,
    source_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
) -> None:
    """Move every article of ``source_ids`` into ``target_id``; sources become ``merged``.

    Edition items pointing at a source event move to the target (or are dropped
    when the target is already in that edition).
    """
    raise NotImplementedError


async def split_event(
    database: AsyncSession,
    *,
    event_id: uuid.UUID,
    article_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
) -> uuid.UUID:
    """Move ``article_ids`` into a new event (``created_by = split``); returns its id."""
    raise NotImplementedError
