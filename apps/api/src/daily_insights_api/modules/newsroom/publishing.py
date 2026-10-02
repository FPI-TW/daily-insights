"""Workstream ③: approval, 09:00 auto-publish, 12:00 late-fill close, edits (spec §6.3).

Owner: editions worktree. The admin API (workstream ④) calls only these
functions for anything that touches translation, analysis or publication;
each writes ``newsroom_edit_log``.

The functions here never commit: the caller owns the transaction. They raise
``LookupError`` for a missing row and ``ValueError`` for a request the current
state does not allow.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom import editlog, queue
from daily_insights_api.modules.newsroom.analysis import filter_related_symbols
from daily_insights_api.modules.newsroom.assembly import review_link
from daily_insights_api.modules.newsroom.models import (
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEvent,
)
from daily_insights_api.modules.newsroom.notifier import Notice
from daily_insights_api.modules.newsroom.translation import (
    live_item_condition,
    mark_english_stale,
    to_zh_hans,
)
from daily_insights_api.modules.newsroom.worker import PeriodicTask, Registration, Runtime

logger = logging.getLogger(__name__)

PUBLISH_SWEEP_INTERVAL = timedelta(minutes=1)


def _now() -> datetime:
    return datetime.now(UTC)


async def _edition_for_update(database: AsyncSession, edition_id: uuid.UUID) -> NewsroomEdition:
    edition = (
        await database.scalars(
            select(NewsroomEdition).where(NewsroomEdition.id == edition_id).with_for_update()
        )
    ).one_or_none()
    if edition is None:
        raise LookupError("newsroom edition not found")
    return edition


async def _event_for_update(database: AsyncSession, event_id: uuid.UUID) -> NewsroomEvent:
    event = (
        await database.scalars(
            select(NewsroomEvent).where(NewsroomEvent.id == event_id).with_for_update()
        )
    ).one_or_none()
    if event is None:
        raise LookupError("newsroom event not found")
    return event


async def _live_event_ids(database: AsyncSession, edition_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await database.scalars(
                select(NewsroomEditionItem.event_id).where(
                    NewsroomEditionItem.edition_id == edition_id, live_item_condition()
                )
            )
        ).all()
    )


async def publish_edition(
    database: AsyncSession,
    edition_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> None:
    """Approve a draft (``user_id``) or auto-publish it (``None``); queues English.

    Publishing an already published edition is a no-op. Items that are not
    ready yet stay in the edition and appear once ready (D15).
    """
    edition = await _edition_for_update(database, edition_id)
    if edition.status == "published":
        return
    moment = now or _now()
    edition.status = "published"
    edition.published_at = moment
    edition.published_by_user_id = user_id
    await database.flush()
    if user_id is not None:
        editlog.record_edit(
            database,
            entity_type="edition",
            entity_id=edition.id,
            action="approve",
            user_id=user_id,
            before={"status": "draft"},
            after={"status": "published", "published_at": moment.isoformat()},
        )
    for event_id in await _live_event_ids(database, edition.id):
        await mark_english_stale(database, event_id)


async def add_event_to_edition(
    database: AsyncSession, edition_id: uuid.UUID, event_id: uuid.UUID, *, user_id: uuid.UUID
) -> uuid.UUID:
    """Append a manual item; queues analysis or a single-market "why" as needed."""
    edition = await _edition_for_update(database, edition_id)
    event = await _event_for_update(database, event_id)
    if event.status != "open":
        raise ValueError("a merged event cannot be added to an edition")
    if event.edition_date != edition.edition_date:
        raise ValueError("the event belongs to another edition date")
    existing = await database.scalar(
        select(NewsroomEditionItem.id).where(
            NewsroomEditionItem.edition_id == edition.id,
            NewsroomEditionItem.event_id == event.id,
        )
    )
    if existing is not None:
        raise ValueError("the event is already in this edition")
    last_rank = await database.scalar(
        select(func.max(NewsroomEditionItem.rank)).where(
            NewsroomEditionItem.edition_id == edition.id
        )
    )
    item = NewsroomEditionItem(
        edition_id=edition.id,
        event_id=event.id,
        rank=(last_rank or 0) + 1,
        origin="manual",
        added_by_user_id=user_id,
        why_status="pending",
    )
    database.add(item)
    await database.flush()
    # A ready event gets this market's "why" from the WHY stage; a pending one
    # writes it with the analysis; anything else needs analysing first.
    if event.analysis_status in ("idle", "failed", "needs_body"):
        await queue.enqueue(database, queue.ANALYSIS, [event.id])
    editlog.record_edit(
        database,
        entity_type="item",
        entity_id=item.id,
        action="add",
        user_id=user_id,
        after={"edition_id": str(edition.id), "event_id": str(event.id), "rank": item.rank},
    )
    return item.id


async def apply_event_edit(
    database: AsyncSession,
    event_id: uuid.UUID,
    *,
    headline: str | None,
    summary: str | None,
    related_symbols: list[dict[str, Any]] | None,
    user_id: uuid.UUID,
) -> None:
    """Save zh-hant edits, regenerate zh-hans, and mark English stale if published.

    ``None`` leaves a field unchanged. Related symbols must all have a site
    dashboard (spec §4.3); an unknown symbol is rejected rather than dropped.
    """
    event = await _event_for_update(database, event_id)
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    if headline is not None:
        text = headline.strip()
        if not text:
            raise ValueError("headline must not be empty")
        before["headline"], after["headline"] = event.headline_zh_hant, text
        event.headline_zh_hant = text
        event.headline_zh_hans = to_zh_hans(text)
    if summary is not None:
        text = summary.strip()
        if not text:
            raise ValueError("summary must not be empty")
        before["summary"], after["summary"] = event.summary_zh_hant, text
        event.summary_zh_hant = text
        event.summary_zh_hans = to_zh_hans(text)
    if related_symbols is not None:
        kept = filter_related_symbols(related_symbols)
        if len(kept) != len(related_symbols):
            raise ValueError("related symbols must each have a site dashboard")
        before["related_symbols"], after["related_symbols"] = event.related_symbols, kept
        event.related_symbols = kept
    if not after:
        return
    event.edited_at = _now()
    event.edited_by_user_id = user_id
    await database.flush()
    editlog.record_edit(
        database,
        entity_type="event",
        entity_id=event.id,
        action="edit",
        user_id=user_id,
        before=before,
        after=after,
    )
    await mark_english_stale(database, event.id)


async def apply_why_edit(
    database: AsyncSession, item_id: uuid.UUID, why: str, *, user_id: uuid.UUID
) -> None:
    """Rewrite one market's zh-hant "why"; zh-hans follows and English is re-queued."""
    text = why.strip()
    if not text:
        raise ValueError("why must not be empty")
    item = (
        await database.scalars(
            select(NewsroomEditionItem).where(NewsroomEditionItem.id == item_id).with_for_update()
        )
    ).one_or_none()
    if item is None:
        raise LookupError("newsroom edition item not found")
    before = {"why": item.why_zh_hant, "why_status": item.why_status}
    item.why_zh_hant = text
    item.why_zh_hans = to_zh_hans(text)
    # A hand-written "why" completes the item; a queued model "why" is cancelled.
    item.why_status = "ready"
    item.why_next_attempt_at = None
    item.why_error_code = None
    await database.flush()
    editlog.record_edit(
        database,
        entity_type="item",
        entity_id=item.id,
        action="edit_why",
        user_id=user_id,
        before=before,
        after={"why": text, "why_status": "ready"},
    )
    await mark_english_stale(database, item.event_id)


async def reanalyze_event(
    database: AsyncSession, event_id: uuid.UUID, *, user_id: uuid.UUID
) -> None:
    """Re-queue analysis for the event and "why" for every item placing it."""
    event = await _event_for_update(database, event_id)
    if event.status != "open":
        raise ValueError("a merged event cannot be re-analysed")
    before = {"analysis_status": event.analysis_status}
    await queue.enqueue(database, queue.ANALYSIS, [event.id])
    item_ids = (
        await database.scalars(
            select(NewsroomEditionItem.id).where(
                NewsroomEditionItem.event_id == event.id, live_item_condition()
            )
        )
    ).all()
    await queue.enqueue(database, queue.WHY, list(item_ids))
    editlog.record_edit(
        database,
        entity_type="event",
        entity_id=event.id,
        action="reanalyze",
        user_id=user_id,
        before=before,
        after={"analysis_status": "pending", "items": [str(item_id) for item_id in item_ids]},
    )


# --- Periodic tasks ----------------------------------------------------------------


async def auto_publish_due(database: AsyncSession, *, now: datetime | None = None) -> int:
    """Publish every draft whose ``auto_publish_at`` has passed (D2, 09:00)."""
    moment = now or _now()
    due = (
        await database.scalars(
            select(NewsroomEdition.id)
            .where(NewsroomEdition.status == "draft", NewsroomEdition.auto_publish_at <= moment)
            .order_by(NewsroomEdition.edition_date, NewsroomEdition.market_code)
        )
    ).all()
    for edition_id in due:
        await publish_edition(database, edition_id, user_id=None, now=moment)
    return len(due)


async def close_late_fill(
    database: AsyncSession, *, now: datetime | None = None
) -> list[tuple[NewsroomEdition, list[str]]]:
    """Abandon items still not ready at the 12:00 deadline (D15).

    Returns each closed edition with the titles of the items it abandoned.
    """
    moment = now or _now()
    editions = (
        await database.scalars(
            select(NewsroomEdition)
            .where(
                NewsroomEdition.status == "published",
                NewsroomEdition.late_fill_deadline <= moment,
                NewsroomEdition.late_fill_closed_at.is_(None),
            )
            .order_by(NewsroomEdition.edition_date, NewsroomEdition.market_code)
            .with_for_update(skip_locked=True)
        )
    ).all()
    closed: list[tuple[NewsroomEdition, list[str]]] = []
    for edition in editions:
        rows = (
            await database.execute(
                select(NewsroomEditionItem.id, NewsroomEvent.working_title)
                .join(NewsroomEvent, NewsroomEvent.id == NewsroomEditionItem.event_id)
                .where(
                    NewsroomEditionItem.edition_id == edition.id,
                    live_item_condition(),
                    or_(
                        NewsroomEvent.analysis_status != "ready",
                        NewsroomEditionItem.why_status != "ready",
                    ),
                )
                .order_by(NewsroomEditionItem.rank)
            )
        ).all()
        abandoned: Sequence[uuid.UUID] = [item_id for item_id, _ in rows]
        if abandoned:
            await database.execute(
                update(NewsroomEditionItem)
                .where(NewsroomEditionItem.id.in_(list(abandoned)))
                .values(abandoned_at=moment)
            )
        edition.late_fill_closed_at = moment
        closed.append((edition, [title for _, title in rows]))
    await database.flush()
    return closed


def register(runtime: Runtime) -> Registration:
    """Periodic tasks: auto-publish due drafts, close late fill at the deadline."""

    async def auto_publish() -> None:
        async with runtime.session_factory() as database:
            published = await auto_publish_due(database)
            await database.commit()
        if published:
            logger.info("newsroom.auto_published", extra={"editions": published})

    async def late_fill() -> None:
        async with runtime.session_factory() as database:
            closed = await close_late_fill(database)
            await database.commit()
        for edition, titles in closed:
            if not titles:
                continue
            await runtime.notifier.send(
                Notice(
                    kind="late_fill_abandoned",
                    title=(
                        f"{edition.edition_date.isoformat()} {edition.market_code} "
                        f"有 {len(titles)} 則新聞在 12:00 前未完成 已放棄"
                    ),
                    lines=tuple(titles),
                    link=review_link(runtime, edition.edition_date),
                )
            )

    return Registration(
        periodic=[
            PeriodicTask("newsroom_auto_publish", PUBLISH_SWEEP_INTERVAL, auto_publish),
            PeriodicTask("newsroom_late_fill_close", PUBLISH_SWEEP_INTERVAL, late_fill),
        ]
    )
