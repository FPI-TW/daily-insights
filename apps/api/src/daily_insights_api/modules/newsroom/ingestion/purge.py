"""Daily body purge: articles first seen more than 30 days ago keep only metadata (D4).

The task runs every few minutes but only has work right after 03:00 Taipei:
the cutoff is "the most recent 03:00 minus 30 days", so it moves once a day,
and ``body_status = 'purged'`` marks rows already done. Nothing depends on
in-memory state, so restarts and several workers are safe.
"""

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.modules.newsroom import clock
from daily_insights_api.modules.newsroom.models import NewsroomArticle

PURGE_BATCH = 1_000


def purge_cutoff(now: datetime) -> datetime:
    local = now.astimezone(clock.TAIPEI)
    day = local.date() if local.time() >= clock.PURGE_AT else local.date() - timedelta(days=1)
    return clock.at_taipei(day, clock.PURGE_AT) - clock.BODY_RETENTION


async def purge_old_bodies(
    session_factory: async_sessionmaker[AsyncSession],
    now: datetime,
    *,
    batch: int = PURGE_BATCH,
) -> int:
    """Null the body of every article older than the cutoff; returns rows purged."""
    cutoff = purge_cutoff(now)
    total = 0
    while True:
        async with session_factory() as database:
            due = (
                select(NewsroomArticle.id)
                .where(
                    NewsroomArticle.first_seen_at < cutoff,
                    NewsroomArticle.body_status != "purged",
                )
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
            purged = (
                await database.scalars(
                    update(NewsroomArticle)
                    .where(NewsroomArticle.id.in_(due.scalar_subquery()))
                    .values(body=None, body_status="purged")
                    .returning(NewsroomArticle.id)
                )
            ).all()
            await database.commit()
        total += len(purged)
        if len(purged) < batch:
            return total
