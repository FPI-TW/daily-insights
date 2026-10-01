"""Workstream ②: window-scoped event search and the event-assignment lock (spec §6.2, D10, D14).

Owner: triage worktree. Events live inside one edition window, so every search
and every write that moves articles between events is scoped to one
``edition_date`` and serialised by ``lock_window``.

The search is exact (no ANN index): a window holds about a thousand articles,
and grouping the cosine distances by event yields distinct candidates directly.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEvent,
    NewsroomSource,
)

CANDIDATE_EVENT_LIMIT = 5
REPRESENTATIVE_TITLE_LIMIT = 2
# A would-be new event joins an event opened while its model call was in flight
# at or above this cosine similarity. Checked under the window lock so two
# concurrent articles about one story cannot both open an event.
SAME_EVENT_SIMILARITY = 0.88
MAX_MERGE_HOPS = 10

# First key of the two-key advisory lock; the second is the edition date ordinal.
_LOCK_NAMESPACE = 0x4E575452


@dataclass(frozen=True, slots=True)
class CandidateEvent:
    id: uuid.UUID
    working_title: str
    similarity: float
    titles: tuple[str, ...] = ()


async def lock_window(database: AsyncSession, edition_date: date) -> None:
    """Serialise event assignment for one window until the transaction ends."""
    await database.execute(
        select(func.pg_advisory_xact_lock(_LOCK_NAMESPACE, edition_date.toordinal()))
    )


async def representative_titles(
    database: AsyncSession,
    event_ids: Sequence[uuid.UUID],
    *,
    limit: int = REPRESENTATIVE_TITLE_LIMIT,
) -> dict[uuid.UUID, tuple[str, ...]]:
    """Up to ``limit`` titles per event, most trusted source first, then earliest seen."""
    if not event_ids:
        return {}
    rank = (
        func.row_number()
        .over(
            partition_by=NewsroomArticle.event_id,
            order_by=(
                NewsroomSource.trust_tier.desc(),
                NewsroomArticle.first_seen_at,
                NewsroomArticle.id,
            ),
        )
        .label("rank")
    )
    ranked = (
        select(NewsroomArticle.event_id, NewsroomArticle.title, rank)
        .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
        .where(NewsroomArticle.event_id.in_(list(event_ids)))
        .subquery()
    )
    rows = (
        await database.execute(
            select(ranked.c.event_id, ranked.c.title)
            .where(ranked.c.rank <= limit)
            .order_by(ranked.c.event_id, ranked.c.rank)
        )
    ).all()
    titles: dict[uuid.UUID, list[str]] = {}
    for event_id, title in rows:
        titles.setdefault(event_id, []).append(title)
    return {event_id: tuple(values) for event_id, values in titles.items()}


async def nearest_events(
    database: AsyncSession,
    *,
    edition_date: date,
    embedding: Sequence[float],
    exclude_article_id: uuid.UUID | None = None,
    exclude_event_ids: Collection[uuid.UUID] = (),
    limit: int = CANDIDATE_EVENT_LIMIT,
    with_titles: bool = True,
) -> list[CandidateEvent]:
    """Open events of the window ranked by their closest article to ``embedding``."""
    distance = func.min(NewsroomArticle.embedding.cosine_distance(list(embedding))).label(
        "distance"
    )
    query = (
        select(NewsroomEvent.id, NewsroomEvent.working_title, distance)
        .join(NewsroomArticle, NewsroomArticle.event_id == NewsroomEvent.id)
        .where(
            NewsroomEvent.edition_date == edition_date,
            NewsroomEvent.status == "open",
            NewsroomArticle.edition_date == edition_date,
            NewsroomArticle.embedding.is_not(None),
        )
        .group_by(NewsroomEvent.id)
        .order_by(distance, NewsroomEvent.id)
        .limit(limit)
    )
    if exclude_article_id is not None:
        query = query.where(NewsroomArticle.id != exclude_article_id)
    if exclude_event_ids:
        query = query.where(NewsroomEvent.id.not_in(list(exclude_event_ids)))
    rows = (await database.execute(query)).all()
    titles = await representative_titles(database, [row.id for row in rows]) if with_titles else {}
    return [
        CandidateEvent(
            id=row.id,
            working_title=row.working_title,
            similarity=1.0 - float(row.distance),
            titles=titles.get(row.id, ()),
        )
        for row in rows
    ]


async def open_event_ids(database: AsyncSession, edition_date: date) -> frozenset[uuid.UUID]:
    """Every open event of the window right now; a snapshot to diff against later."""
    return frozenset(
        (
            await database.scalars(
                select(NewsroomEvent.id).where(
                    NewsroomEvent.edition_date == edition_date, NewsroomEvent.status == "open"
                )
            )
        ).all()
    )


async def resolve_open_event(database: AsyncSession, event_id: uuid.UUID) -> uuid.UUID | None:
    """Follow ``merged_into_id`` to the open event that absorbed ``event_id``, if any."""
    current: uuid.UUID | None = event_id
    for _ in range(MAX_MERGE_HOPS):
        if current is None:
            return None
        row = (
            await database.execute(
                select(NewsroomEvent.status, NewsroomEvent.merged_into_id).where(
                    NewsroomEvent.id == current
                )
            )
        ).one_or_none()
        if row is None:
            return None
        if row.status == "open":
            return current
        current = row.merged_into_id
    return None
