"""Operator recovery after a provider outage (runbook "重新排入").

``requeue`` puts the failed rows of one stage back on their queue, by error
code and edition date. ``reembed_untranslated`` embeds again the articles
whose embed-stage translation failed for good, so they were embedded as
written. Both return a plan of the rows they touch per edition date; the rows
change only with ``apply=True``, and the caller owns the transaction. Rows a
worker may be processing are never touched (``queue.in_flight``).
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from daily_insights_api.modules.newsroom import queue
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEvent,
    NewsroomLlmCall,
)
from daily_insights_api.modules.newsroom.translation import live_item_condition

# The runbook's stage names; ``en`` is the TRANSLATE queue's column prefix.
REQUEUE_STAGES: dict[str, queue.StageSpec] = {
    "fetch": queue.FETCH,
    "embed": queue.EMBED,
    "triage": queue.TRIAGE,
    "analysis": queue.ANALYSIS,
    "why": queue.WHY,
    "en": queue.TRANSLATE,
}


@dataclass(frozen=True, slots=True)
class Group:
    """Rows of one edition date: the queue they go to (``None``: left alone) and why."""

    edition_date: date
    queue: str | None
    reason: str
    row_ids: tuple[uuid.UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "edition_date": self.edition_date.isoformat(),
            "queue": self.queue,
            "reason": self.reason,
            "count": len(self.row_ids),
        }


@dataclass
class Plan:
    groups: list[Group] = field(default_factory=list)
    applied: bool = False

    @property
    def queued(self) -> int:
        return sum(len(group.row_ids) for group in self.groups if group.queue is not None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "queued": self.queued,
            "groups": [group.as_dict() for group in self.groups],
        }


def _plan(rows: Iterable[tuple[uuid.UUID, date, str | None, str]]) -> Plan:
    grouped: dict[tuple[date, str | None, str], list[uuid.UUID]] = defaultdict(list)
    for row_id, edition_date, target, reason in rows:
        grouped[(edition_date, target, reason)].append(row_id)
    return Plan(
        [
            Group(edition_date, target, reason, tuple(ids))
            for (edition_date, target, reason), ids in sorted(
                grouped.items(), key=lambda entry: (entry[0][0], entry[0][1] or "~", entry[0][2])
            )
        ]
    )


def _dates(column: Any, start: date | None, end: date | None) -> list[Any]:
    conditions = []
    if start is not None:
        conditions.append(column >= start)
    if end is not None:
        conditions.append(column <= end)
    return conditions


def untranslated_embedding(embedding_model: str) -> ColumnElement[bool]:
    """Articles embedded as written because their embed-stage translation failed.

    The translation call is audited under stage ``embed`` with the translation
    model, so any other model than ``embedding_model``; a provider error there
    has an ``embed_provider_*`` code. A later successful translation (a stage
    retry, or ``run_newsroom_reembed``) means the article is translated now.
    """
    failed = aliased(NewsroomLlmCall)
    later = aliased(NewsroomLlmCall)
    # Auto-correlation reaches only the enclosing query; the article is two up.
    retranslated = (
        exists()
        .where(
            later.stage == "embed",
            later.subject_id == NewsroomArticle.id,
            later.model != embedding_model,
            later.error_code.is_(None),
            later.created_at > failed.created_at,
        )
        .correlate(NewsroomArticle, failed)
    )
    return exists().where(
        failed.stage == "embed",
        failed.subject_id == NewsroomArticle.id,
        failed.model != embedding_model,
        failed.error_code.startswith("embed_provider_", autoescape=True),
        ~retranslated,
    )


async def _back_to_embed(database: AsyncSession, article_ids: Sequence[uuid.UUID]) -> None:
    """Embed again; triage waits for the new vector, whose success queues it."""
    await queue.enqueue(database, queue.EMBED, article_ids)
    await queue.park(database, queue.TRIAGE, article_ids)


async def _failed_articles(
    database: AsyncSession,
    stage: str,
    error_codes: Sequence[str],
    *,
    start: date | None,
    end: date | None,
    embedding_model: str,
    now: datetime,
    lock: bool,
) -> list[tuple[uuid.UUID, date, str | None, str]]:
    spec = REQUEUE_STAGES[stage]
    conditions = [
        queue.failed_with(spec, error_codes),
        *_dates(NewsroomArticle.edition_date, start, end),
    ]
    if stage != "triage":
        plain = select(NewsroomArticle.id, NewsroomArticle.edition_date).where(*conditions)
        if lock:
            plain = plain.with_for_update(of=NewsroomArticle)
        return [
            (row.id, row.edition_date, stage, "failed") for row in await database.execute(plain)
        ]
    query = select(
        NewsroomArticle.id,
        NewsroomArticle.edition_date,
        NewsroomArticle.embedding.is_(None).label("missing"),
        untranslated_embedding(embedding_model).label("untranslated"),
        queue.in_flight(queue.EMBED, now).label("embedding"),
    ).where(*conditions)
    if lock:
        query = query.with_for_update(of=NewsroomArticle)
    rows: list[tuple[uuid.UUID, date, str | None, str]] = []
    for row in await database.execute(query):
        if not (row.missing or row.untranslated):
            rows.append((row.id, row.edition_date, stage, "failed"))
        elif row.embedding:
            rows.append((row.id, row.edition_date, None, "embed_in_flight"))
        else:
            # Triage needs a vector of the translated title; it runs again after embed.
            reason = "embedding_missing" if row.missing else "untranslated_embedding"
            rows.append((row.id, row.edition_date, "embed", reason))
    return rows


async def _failed_events(
    database: AsyncSession,
    stage: str,
    error_codes: Sequence[str],
    *,
    start: date | None,
    end: date | None,
    lock: bool,
) -> list[tuple[uuid.UUID, date, str | None, str]]:
    spec = REQUEUE_STAGES[stage]
    query = select(NewsroomEvent.id, NewsroomEvent.edition_date, NewsroomEvent.status).where(
        queue.failed_with(spec, error_codes), *_dates(NewsroomEvent.edition_date, start, end)
    )
    if lock:
        query = query.with_for_update(of=NewsroomEvent)
    return [
        (row.id, row.edition_date, stage, "failed")
        if row.status == "open"
        else (row.id, row.edition_date, None, "event_merged")
        for row in await database.execute(query)
    ]


async def _failed_items(
    database: AsyncSession,
    error_codes: Sequence[str],
    *,
    start: date | None,
    end: date | None,
    lock: bool,
) -> list[tuple[uuid.UUID, date, str | None, str]]:
    query = (
        select(
            NewsroomEditionItem.id,
            NewsroomEdition.edition_date,
            live_item_condition().label("live"),
            NewsroomEvent.analysis_status,
        )
        .join(NewsroomEdition, NewsroomEdition.id == NewsroomEditionItem.edition_id)
        .join(NewsroomEvent, NewsroomEvent.id == NewsroomEditionItem.event_id)
        .where(
            queue.failed_with(queue.WHY, error_codes),
            *_dates(NewsroomEdition.edition_date, start, end),
        )
    )
    if lock:
        query = query.with_for_update(of=NewsroomEditionItem)
    rows: list[tuple[uuid.UUID, date, str | None, str]] = []
    for row in await database.execute(query):
        if not row.live:
            rows.append((row.id, row.edition_date, None, "item_not_live"))
        elif row.analysis_status != "ready":
            # The WHY stage only takes analysed events; the analysis writes this "why".
            rows.append((row.id, row.edition_date, None, "event_not_ready"))
        else:
            rows.append((row.id, row.edition_date, "why", "failed"))
    return rows


async def requeue(
    database: AsyncSession,
    stage: str,
    error_codes: Sequence[str],
    *,
    start: date | None = None,
    end: date | None = None,
    embedding_model: str,
    apply: bool = False,
    now: datetime | None = None,
) -> Plan:
    """Put ``failed`` rows of ``stage`` with one of ``error_codes`` back on the queue.

    The same reset as ``queue.enqueue`` and the runbook's SQL. A failed triage
    whose article has no embedding, or one of the article's untranslated
    title, goes back to ``embed`` instead. Analysis and translation skip merged
    events; a "why" is requeued only for a live item of an analysed event.
    """
    if stage not in REQUEUE_STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    if not error_codes:
        raise ValueError("at least one error code is required")
    moment = now or datetime.now(UTC)
    if stage in ("fetch", "embed", "triage"):
        rows = await _failed_articles(
            database,
            stage,
            error_codes,
            start=start,
            end=end,
            embedding_model=embedding_model,
            now=moment,
            lock=apply,
        )
    elif stage == "why":
        rows = await _failed_items(database, error_codes, start=start, end=end, lock=apply)
    else:
        rows = await _failed_events(database, stage, error_codes, start=start, end=end, lock=apply)
    plan = _plan(rows)
    if not apply:
        return plan
    spec = REQUEUE_STAGES[stage]
    for group in plan.groups:
        if group.queue == stage:
            await queue.requeue_failed(database, spec, group.row_ids)
        elif group.queue == "embed":
            await _back_to_embed(database, group.row_ids)
    plan.applied = True
    return plan


async def reembed_untranslated(
    database: AsyncSession,
    *,
    start: date | None = None,
    end: date | None = None,
    embedding_model: str,
    apply: bool = False,
    now: datetime | None = None,
) -> Plan:
    """Embed again the not yet triaged articles embedded with an untranslated title.

    A triaged article keeps its event (listed, never touched); one whose
    triage is in flight is left for that attempt.
    """
    moment = now or datetime.now(UTC)
    query = select(
        NewsroomArticle.id,
        NewsroomArticle.edition_date,
        NewsroomArticle.triage_status,
        queue.in_flight(queue.TRIAGE, moment).label("triaging"),
    ).where(
        untranslated_embedding(embedding_model),
        NewsroomArticle.embed_status == "done",
        *_dates(NewsroomArticle.edition_date, start, end),
    )
    if apply:
        query = query.with_for_update(of=NewsroomArticle)
    rows: list[tuple[uuid.UUID, date, str | None, str]] = []
    for row in await database.execute(query):
        if row.triage_status == "done":
            rows.append((row.id, row.edition_date, None, "already_triaged"))
        elif row.triaging:
            rows.append((row.id, row.edition_date, None, "triage_in_flight"))
        else:
            rows.append((row.id, row.edition_date, "embed", "untranslated_embedding"))
    plan = _plan(rows)
    if apply:
        for group in plan.groups:
            if group.queue == "embed":
                await _back_to_embed(database, group.row_ids)
        plan.applied = True
    return plan
