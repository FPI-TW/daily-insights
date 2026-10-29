"""Workstream ③: OpenCC zh-hans conversion and async English translation (spec D7).

Owner: editions worktree.

zh-hant is the only editable language. zh-hans is derived synchronously with
OpenCC ``tw2sp`` wherever zh-hant is written; English is produced by the
``queue.TRANSLATE`` stage. ``en_source_digest`` records which zh-hant headline
and summary the English came from, so changing either makes the English stale
for readers (spec §4.3, §4.5). "Why it matters" is shown in the admin console
only: rewriting one resets its ``why_en_status``, and the English "why" is
translated again without hiding the reader-facing English.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
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

TRANSLATE_PROMPT_VERSION = "newsroom.translate.v2"
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


def zh_hant_digest(headline: str | None, summary: str | None) -> str:
    """SHA-256 of the zh-hant headline and summary an English translation is made from.

    The reader API recomputes this to decide whether the English is current.
    """
    payload = json.dumps(
        {"headline": headline, "summary": summary},
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
            live_item_condition(),
        )
        .order_by(NewsroomEditionItem.id)
    )


async def _visible_items(
    database: AsyncSession, event_id: uuid.UUID
) -> Sequence[NewsroomEditionItem]:
    return (await database.scalars(visible_items_query(event_id))).all()


async def current_zh_hant_digest(database: AsyncSession, event_id: uuid.UUID) -> str:
    """``zh_hant_digest`` of the event's current zh-hant headline and summary."""
    event = await database.get(NewsroomEvent, event_id)
    if event is None:
        raise LookupError("newsroom event not found")
    return zh_hant_digest(event.headline_zh_hant, event.summary_zh_hant)


def _ready_whys(items: Sequence[NewsroomEditionItem]) -> dict[str, str]:
    """The finished zh-hant "why" of each item that has one, by item id (string).

    Migrated legacy items and items still writing theirs have none to translate.
    """
    return {
        str(item.id): item.why_zh_hant
        for item in items
        if item.why_status == "ready" and item.why_zh_hant
    }


def _english_is_current(
    event: NewsroomEvent, items: Sequence[NewsroomEditionItem], digest: str
) -> bool:
    whys = _ready_whys(items)
    return _card_is_current(event, digest) and all(
        item.why_en_status == "ready" and item.why_en for item in items if str(item.id) in whys
    )


def _card_is_current(event: NewsroomEvent, digest: str) -> bool:
    return (
        event.en_source_digest == digest
        and event.headline_en is not None
        and event.summary_en is not None
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
    digest = zh_hant_digest(event.headline_zh_hant, event.summary_zh_hant)
    if event.en_status == "ready" and _english_is_current(event, items, digest):
        return False
    await queue.enqueue(database, queue.TRANSLATE, [event_id])
    return True


async def queue_english_after_analysis(database: AsyncSession, event_id: uuid.UUID) -> bool:
    """Queue English for an event whose analysis settles new zh-hant in this transaction.

    ``mark_english_stale`` waits for ``analysis_status = ready``, which the
    analysis settle writes only after its handler returns, and the sweep only
    covers recent editions; without this, re-analysing an older published
    edition would hide its English for good. Returns whether it queued.
    """
    if not await _visible_items(database, event_id):
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
    """``queue.TRANSLATE`` handler: English headline, summary and finished "why"s."""
    event = await database.get(NewsroomEvent, claim_.row_id)
    if event is None:
        raise RetryableStageError("translate_event_missing")
    if event.analysis_status != "ready" or not event.headline_zh_hant or not event.summary_zh_hant:
        # Nothing final to translate yet; analysis re-queues English once ready.
        return {"en_status": "idle"}
    items = await _visible_items(database, event.id)
    whys = _ready_whys([item for item in items if item.why_en_status != "ready" or not item.why_en])
    digest = zh_hant_digest(event.headline_zh_hant, event.summary_zh_hant)
    translate_card = not _card_is_current(event, digest)
    if not translate_card and not whys:
        return {}
    payload: dict[str, Any] = {"why": whys}
    if translate_card:
        payload.update(headline=event.headline_zh_hant, summary=event.summary_zh_hant)
    result = await runtime.llm.complete(
        database,
        model=runtime.settings.newsroom_translate_model,
        system=load_prompt("translate"),
        payload=payload,
        result_type=TranslationResult,
        audit=CallAudit("translate", event.id, TRANSLATE_PROMPT_VERSION),
    )
    expected = {"why", "headline", "summary"} if translate_card else {"why"}
    if (
        set(result.why) != whys.keys()
        or result.model_fields_set != expected
        or (translate_card and (result.headline is None or result.summary is None))
    ):
        raise contract_error(database, "translate_schema_invalid")
    for item_id, translated in result.why.items():
        await database.execute(
            update(NewsroomEditionItem)
            .where(
                NewsroomEditionItem.id == uuid.UUID(item_id),
                NewsroomEditionItem.why_zh_hant == whys[item_id],
                NewsroomEditionItem.why_status == "ready",
            )
            .values(why_en=translated, why_en_status="ready")
        )
    if translate_card:
        return {
            "headline_en": result.headline,
            "summary_en": result.summary,
            "en_source_digest": digest,
        }
    return {}


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
