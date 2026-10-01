"""Workstream ③: OpenCC zh-hans conversion and async English translation (spec D7).

Owner: editions worktree.

zh-hant is the only editable language. zh-hans is derived synchronously with
OpenCC ``tw2sp`` wherever zh-hant is written; English is produced by the
``queue.TRANSLATE`` stage. ``en_source_digest`` records which zh-hant text the
English came from, so a changed headline, summary or visible "why" makes the
English stale (spec §4.3, §4.5).
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib.resources import files
from typing import Any

import opencc
from sqlalchemy import Select, and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom import queue
from daily_insights_api.modules.newsroom.contracts import TranslationResult
from daily_insights_api.modules.newsroom.models import (
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEvent,
    NewsroomLlmCall,
)
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.queue import Claim, RetryableStageError
from daily_insights_api.modules.newsroom.worker import (
    PeriodicTask,
    Registration,
    Runtime,
    StageBinding,
)

logger = logging.getLogger(__name__)

TRANSLATE_PROMPT_VERSION = "newsroom.translate.v1"
STALE_SWEEP_INTERVAL = timedelta(minutes=1)
# Only recent editions are swept; older English is left as it is.
STALE_SWEEP_LOOKBACK = timedelta(days=2)


@cache
def _converter() -> opencc.OpenCC:
    return opencc.OpenCC("tw2sp")


def to_zh_hans(text: str) -> str:
    """Convert Taiwan-standard Traditional Chinese to Simplified (OpenCC ``tw2sp``)."""
    return str(_converter().convert(text))


def to_zh_hans_optional(text: str | None) -> str | None:
    return None if text is None else to_zh_hans(text)


@cache
def load_prompt(name: str) -> str:
    """Read ``prompts/<name>.txt`` shipped inside the newsroom package."""
    resource = files("daily_insights_api.modules.newsroom").joinpath("prompts", f"{name}.txt")
    return resource.read_text(encoding="utf-8").strip()


def contract_error(database: AsyncSession, code: str) -> RetryableStageError:
    """A model answer that validated but broke the stage contract (ids missing or extra).

    The provider already added a successful audit row to ``database``; it is
    marked with ``code`` and moved onto the error so it survives the stage's
    rollback (``queue.run_claimed`` re-adds ``audit_rows``).
    """
    error = RetryableStageError(code)
    for row in [row for row in database.new if isinstance(row, NewsroomLlmCall)]:
        row.error_code = code
        database.expunge(row)
        error.audit_rows.append(row)
    return error


def zh_hant_digest(
    headline: str | None, summary: str | None, whys: Mapping[str, str | None]
) -> str:
    """SHA-256 of the zh-hant text an English translation is made from.

    ``whys`` maps edition item id (string) to that item's ``why_zh_hant`` for
    every reader-visible item of the event (see ``visible_items_query``). The
    reader API recomputes this to decide whether the English is current.
    """
    payload = json.dumps(
        {"headline": headline, "summary": summary, "whys": dict(whys)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def live_item_condition() -> Any:
    """Items still in play: not removed in draft, not hidden, not abandoned."""
    return and_(
        NewsroomEditionItem.removed_at.is_(None),
        NewsroomEditionItem.hidden_at.is_(None),
        NewsroomEditionItem.abandoned_at.is_(None),
    )


def visible_items_query(event_id: uuid.UUID) -> Select[tuple[NewsroomEditionItem]]:
    """The event's items a reader can see once the event analysis is ready (spec §4.5)."""
    return (
        select(NewsroomEditionItem)
        .join(NewsroomEdition, NewsroomEdition.id == NewsroomEditionItem.edition_id)
        .where(
            NewsroomEditionItem.event_id == event_id,
            NewsroomEdition.status == "published",
            NewsroomEditionItem.why_status == "ready",
            live_item_condition(),
        )
        .order_by(NewsroomEditionItem.id)
    )


async def _visible_items(
    database: AsyncSession, event_id: uuid.UUID
) -> Sequence[NewsroomEditionItem]:
    return (await database.scalars(visible_items_query(event_id))).all()


async def current_zh_hant_digest(database: AsyncSession, event_id: uuid.UUID) -> str:
    """``zh_hant_digest`` of the event's current zh-hant text and visible items."""
    event = await database.get(NewsroomEvent, event_id)
    if event is None:
        raise LookupError("newsroom event not found")
    items = await _visible_items(database, event_id)
    return zh_hant_digest(
        event.headline_zh_hant,
        event.summary_zh_hant,
        {str(item.id): item.why_zh_hant for item in items},
    )


def _english_is_current(
    event: NewsroomEvent, items: Sequence[NewsroomEditionItem], digest: str
) -> bool:
    return (
        event.en_source_digest == digest
        and event.headline_en is not None
        and event.summary_en is not None
        and all(item.why_en_status == "ready" and item.why_en for item in items)
    )


async def mark_english_stale(database: AsyncSession, event_id: uuid.UUID) -> bool:
    """Queue English for an event whose visible zh-hant changed (spec §5.1).

    Does nothing while no item of the event is visible (publishing queues it
    then), or while the existing English already matches. Returns whether the
    event was queued.
    """
    event = await database.get(NewsroomEvent, event_id)
    if event is None or event.analysis_status != "ready":
        return False
    items = await _visible_items(database, event_id)
    if not items:
        return False
    digest = zh_hant_digest(
        event.headline_zh_hant,
        event.summary_zh_hant,
        {str(item.id): item.why_zh_hant for item in items},
    )
    if event.en_status == "ready" and _english_is_current(event, items, digest):
        return False
    await queue.enqueue(database, queue.TRANSLATE, [event_id])
    return True


async def sweep_stale_english(database: AsyncSession, *, now: datetime | None = None) -> int:
    """Queue English for recently published events whose zh-hant drifted.

    Catches changes no publishing function sees directly: an item finishing
    its "why" after 09:00, or an admin hiding or removing an item. Events in
    ``pending`` are already queued and ``failed`` ones wait for a new edit, so
    only ``idle`` and ``ready`` events are considered.
    """
    moment = now or datetime.now(UTC)
    event_ids = (
        await database.scalars(
            select(NewsroomEditionItem.event_id)
            .join(NewsroomEdition, NewsroomEdition.id == NewsroomEditionItem.edition_id)
            .join(NewsroomEvent, NewsroomEvent.id == NewsroomEditionItem.event_id)
            .where(
                NewsroomEdition.status == "published",
                NewsroomEdition.published_at >= moment - STALE_SWEEP_LOOKBACK,
                NewsroomEvent.analysis_status == "ready",
                NewsroomEvent.en_status.in_(("idle", "ready")),
            )
            .distinct()
        )
    ).all()
    queued = 0
    for event_id in event_ids:
        if await mark_english_stale(database, event_id):
            queued += 1
    return queued


async def translate_event(
    runtime: Runtime, database: AsyncSession, claim_: Claim
) -> dict[str, Any]:
    """``queue.TRANSLATE`` handler: English headline, summary and visible "why"s."""
    event = await database.get(NewsroomEvent, claim_.row_id)
    if event is None:
        raise RetryableStageError("translate_event_missing")
    if event.analysis_status != "ready" or not event.headline_zh_hant or not event.summary_zh_hant:
        # Nothing final to translate yet; analysis re-queues English once ready.
        return {"en_status": "idle"}
    items = await _visible_items(database, event.id)
    whys = {str(item.id): item.why_zh_hant for item in items}
    digest = zh_hant_digest(event.headline_zh_hant, event.summary_zh_hant, whys)
    if _english_is_current(event, items, digest):
        return {}
    result = await runtime.llm.complete(
        database,
        model=runtime.settings.newsroom_translate_model,
        system=load_prompt("translate"),
        payload={
            "headline": event.headline_zh_hant,
            "summary": event.summary_zh_hant,
            "why": {key: value for key, value in whys.items() if value},
        },
        result_type=TranslationResult,
        audit=CallAudit("translate", event.id, TRANSLATE_PROMPT_VERSION),
    )
    expected = {key for key, value in whys.items() if value}
    if set(result.why) != expected:
        raise contract_error(database, "translate_schema_invalid")
    for item in items:
        translated = result.why.get(str(item.id))
        await database.execute(
            update(NewsroomEditionItem)
            .where(NewsroomEditionItem.id == item.id)
            .values(why_en=translated, why_en_status="ready" if translated else "idle")
        )
    return {
        "headline_en": result.headline,
        "summary_en": result.summary,
        "en_source_digest": digest,
    }


def register(runtime: Runtime) -> Registration:
    """Stage binding for ``queue.TRANSLATE`` plus the stale-English sweep."""

    async def handler(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
        return await translate_event(runtime, database, claim_)

    async def sweep() -> None:
        async with runtime.session_factory() as database:
            queued = await sweep_stale_english(database)
            await database.commit()
        if queued:
            logger.info("newsroom.english_requeued", extra={"events": queued})

    return Registration(
        stages=[StageBinding(queue.TRANSLATE, handler)],
        periodic=[PeriodicTask("newsroom_english_stale_sweep", STALE_SWEEP_INTERVAL, sweep)],
    )
