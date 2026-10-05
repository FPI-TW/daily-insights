"""Workstream ②: admin event merge and split (spec §6.2, D10).

Owner: triage worktree. Both operations re-queue analysis for affected events
that sit in an edition, and write ``newsroom_edit_log``. They hold the window's
assignment lock (``clustering.lock_window``) so triage never attaches an article
to an event mid-change. The caller owns the transaction and commits.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom import clustering, editlog, queue
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEditionItem,
    NewsroomEvent,
)

logger = logging.getLogger(__name__)


class EventServiceError(ValueError):
    """An admin request that cannot be applied; ``code`` is stable for the API layer.

    Codes: ``event_not_found``, ``event_not_open``, ``edition_date_mismatch``,
    ``invalid_merge_sources``, ``empty_split``, ``article_not_in_event``,
    ``split_takes_every_article``.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _open_events(
    database: AsyncSession, event_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, NewsroomEvent]:
    events = {
        event.id: event
        for event in (
            await database.scalars(
                select(NewsroomEvent)
                .where(NewsroomEvent.id.in_(list(event_ids)))
                .order_by(NewsroomEvent.id)
                .with_for_update()
            )
        ).all()
    }
    if len(events) != len(set(event_ids)):
        raise EventServiceError("event_not_found")
    if any(event.status != "open" for event in events.values()):
        raise EventServiceError("event_not_open")
    return events


async def _edition_date(database: AsyncSession, event_id: uuid.UUID) -> date:
    edition_date = await database.scalar(
        select(NewsroomEvent.edition_date).where(NewsroomEvent.id == event_id)
    )
    if edition_date is None:
        raise EventServiceError("event_not_found")
    return edition_date


async def _article_ids(database: AsyncSession, event_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await database.scalars(
                select(NewsroomArticle.id)
                .where(NewsroomArticle.event_id == event_id)
                .order_by(NewsroomArticle.first_seen_at, NewsroomArticle.id)
            )
        ).all()
    )


async def _requeue_if_placed(database: AsyncSession, event_ids: Sequence[uuid.UUID]) -> list[str]:
    """Re-queue analysis for events with a live item in some edition (spec §5.1)."""
    placed = list(
        (
            await database.scalars(
                select(NewsroomEditionItem.event_id)
                .where(
                    NewsroomEditionItem.event_id.in_(list(event_ids)),
                    NewsroomEditionItem.removed_at.is_(None),
                )
                .distinct()
            )
        ).all()
    )
    await queue.enqueue(database, queue.ANALYSIS, placed)
    return sorted(str(event_id) for event_id in placed)


def _ids(values: Sequence[uuid.UUID]) -> list[str]:
    return [str(value) for value in values]


async def merge_events(
    database: AsyncSession,
    *,
    target_id: uuid.UUID,
    source_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID | None,
) -> None:
    """Move every article of ``source_ids`` into ``target_id``; sources become ``merged``.

    ``user_id`` is ``None`` for the assembly's automatic duplicate merge, which
    logs instead of writing the admin edit log (that log requires an admin).

    Edition items pointing at a source event move to the target (or are dropped
    when the target is already in that edition; a removed target item is then
    restored at the live source item's rank).
    """
    sources = list(dict.fromkeys(source_ids))
    if not sources or target_id in sources:
        raise EventServiceError("invalid_merge_sources")
    await clustering.lock_window(database, await _edition_date(database, target_id))
    events = await _open_events(database, [target_id, *sources])
    target = events[target_id]
    if any(events[source].edition_date != target.edition_date for source in sources):
        raise EventServiceError("edition_date_mismatch")

    moved_articles = list(
        (
            await database.scalars(
                select(NewsroomArticle.id)
                .where(NewsroomArticle.event_id.in_(sources))
                .order_by(NewsroomArticle.first_seen_at, NewsroomArticle.id)
            )
        ).all()
    )
    await database.execute(
        update(NewsroomArticle)
        .where(NewsroomArticle.event_id.in_(sources))
        .values(event_id=target_id)
    )

    target_items = {
        item.edition_id: item
        for item in (
            await database.scalars(
                select(NewsroomEditionItem)
                .where(NewsroomEditionItem.event_id == target_id)
                .with_for_update()
            )
        ).all()
    }
    items = (
        await database.scalars(
            select(NewsroomEditionItem)
            .where(NewsroomEditionItem.event_id.in_(sources))
            .order_by(NewsroomEditionItem.created_at, NewsroomEditionItem.id)
            .with_for_update()
        )
    ).all()
    now = datetime.now(UTC)
    repointed: list[uuid.UUID] = []
    removed: list[uuid.UUID] = []
    restored: list[uuid.UUID] = []
    for item in items:
        target_item = target_items.get(item.edition_id)
        if target_item is None:
            item.event_id = target_id
            target_items[item.edition_id] = item
            repointed.append(item.id)
            continue
        # (edition, event) is unique, so the target's own item stays the
        # edition's entry and the source's item leaves the draft. A live source
        # item revives a removed target item in its place.
        if item.removed_at is None:
            if target_item.removed_at is not None:
                target_item.removed_at = None
                target_item.rank = item.rank
                restored.append(target_item.id)
            item.removed_at = now
            removed.append(item.id)
    await database.flush()

    # Keep merge chains one hop deep: events merged into a source now point at the target.
    await database.execute(
        update(NewsroomEvent)
        .where(NewsroomEvent.merged_into_id.in_(sources))
        .values(merged_into_id=target_id)
    )
    for source in sources:
        events[source].status = "merged"
        events[source].merged_into_id = target_id
    await database.flush()
    requeued = await _requeue_if_placed(database, [target_id])
    if user_id is None:
        logger.info(
            "newsroom.events_auto_merged",
            extra={"target_event_id": str(target_id), "source_event_ids": _ids(sources)},
        )
        return

    editlog.record_edit(
        database,
        entity_type="event",
        entity_id=target_id,
        action="merge",
        user_id=user_id,
        before={"source_event_ids": _ids(sources)},
        after={
            "moved_article_ids": _ids(moved_articles),
            "repointed_item_ids": _ids(repointed),
            "removed_item_ids": _ids(removed),
            "restored_item_ids": _ids(restored),
            "analysis_requeued_event_ids": requeued,
        },
    )
    for source in sources:
        editlog.record_edit(
            database,
            entity_type="event",
            entity_id=source,
            action="merged_into",
            user_id=user_id,
            before={"status": "open", "merged_into_id": None},
            after={"status": "merged", "merged_into_id": str(target_id)},
        )


async def split_event(
    database: AsyncSession,
    *,
    event_id: uuid.UUID,
    article_ids: Sequence[uuid.UUID],
    user_id: uuid.UUID,
) -> uuid.UUID:
    """Move ``article_ids`` into a new event (``created_by = split``); returns its id."""
    moving = list(dict.fromkeys(article_ids))
    if not moving:
        raise EventServiceError("empty_split")
    await clustering.lock_window(database, await _edition_date(database, event_id))
    original = (await _open_events(database, [event_id]))[event_id]
    current = await _article_ids(database, event_id)
    if not set(moving) <= set(current):
        raise EventServiceError("article_not_in_event")
    if len(moving) == len(current):
        raise EventServiceError("split_takes_every_article")

    titles = await database.scalars(
        select(NewsroomArticle.title)
        .where(NewsroomArticle.id.in_(moving))
        .order_by(NewsroomArticle.first_seen_at, NewsroomArticle.id)
        .limit(1)
    )
    working_title = (titles.first() or original.working_title)[:500]
    created = NewsroomEvent(
        edition_date=original.edition_date,
        working_title=working_title,
        status="open",
        created_by="split",
    )
    database.add(created)
    await database.flush()
    await database.execute(
        update(NewsroomArticle).where(NewsroomArticle.id.in_(moving)).values(event_id=created.id)
    )
    requeued = await _requeue_if_placed(database, [event_id, created.id])
    remaining = await database.scalar(
        select(func.count())
        .select_from(NewsroomArticle)
        .where(NewsroomArticle.event_id == event_id)
    )

    editlog.record_edit(
        database,
        entity_type="event",
        entity_id=event_id,
        action="split",
        user_id=user_id,
        before={"article_ids": _ids(current)},
        after={
            "new_event_id": str(created.id),
            "moved_article_ids": _ids(moving),
            "remaining_article_count": remaining,
            "analysis_requeued_event_ids": requeued,
        },
    )
    editlog.record_edit(
        database,
        entity_type="event",
        entity_id=created.id,
        action="split_from",
        user_id=user_id,
        before=None,
        after={
            "source_event_id": str(event_id),
            "article_ids": _ids(moving),
            "working_title": working_title,
        },
    )
    return created.id
