"""Shared date/object locks and durable upload-generation fences.

Lock order: date, episode/batch rows, object advisory lock, asset row.
Cleanup uses only object locks, so it never waits for a date/episode lock.
"""

import hashlib
import uuid
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.podcasts.models import PodcastDateGeneration, PodcastEpisode

PODCAST_UPLOAD_LOCK_NAMESPACE = 0x504F4443


async def lock_upload_date(database: AsyncSession, trading_date: date) -> None:
    await database.execute(
        select(func.pg_advisory_xact_lock(PODCAST_UPLOAD_LOCK_NAMESPACE, trading_date.toordinal()))
    )


async def lock_upload_object(database: AsyncSession, asset_id: uuid.UUID) -> None:
    key = int.from_bytes(hashlib.sha256(asset_id.bytes).digest()[:8], "big", signed=True)
    await database.execute(select(func.pg_advisory_xact_lock(key)))


async def date_generation(database: AsyncSession, trading_date: date) -> int:
    return (
        await database.scalar(
            select(PodcastDateGeneration.generation).where(
                PodcastDateGeneration.trading_date == trading_date
            )
        )
        or 0
    )


async def require_generation(database: AsyncSession, trading_date: date, generation: int) -> None:
    if generation != await date_generation(database, trading_date):
        raise HTTPException(409, detail={"code": "upload_generation_conflict"})


def require_mutable(episode: PodcastEpisode) -> None:
    if episode.deletion_pending:
        raise HTTPException(
            409, detail={"code": "episode_removal_pending", "current_version": episode.version}
        )
