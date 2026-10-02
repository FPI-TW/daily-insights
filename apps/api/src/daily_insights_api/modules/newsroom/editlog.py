"""Shared helper for the append-only admin action log (spec §4.6)."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom.models import NewsroomEditLog


def record_edit(
    database: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    action: str,
    user_id: uuid.UUID,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    database.add(
        NewsroomEditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            before=before,
            after=after,
            user_id=user_id,
        )
    )
