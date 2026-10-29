"""Workstream ②: embedding and per-article triage with event assignment (spec §6.2).

Owner: triage worktree. EMBED turns title + feed summary into a vector and hands
the article to TRIAGE (§5.1). TRIAGE scores the article, then attaches it to an
event of its edition window; the model call runs concurrently, the event write
is serialised per window by ``clustering.lock_window``.
"""

import hashlib
import logging
import re
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import cache
from importlib.resources import files
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom import clock, clustering, queue, scoring
from daily_insights_api.modules.newsroom.clustering import CandidateEvent
from daily_insights_api.modules.newsroom.contracts import (
    EmbeddingTranslation,
    EventMatch,
    EventNew,
    TriageResult,
)
from daily_insights_api.modules.newsroom.ingestion.allowlist import MANUAL_KIND
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEvent,
    NewsroomLlmCall,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.queue import (
    Claim,
    FatalStageError,
    RetryableStageError,
    StageHandler,
)
from daily_insights_api.modules.newsroom.variants import SelectionVariant, get_variant
from daily_insights_api.modules.newsroom.worker import Registration, Runtime, StageBinding

logger = logging.getLogger(__name__)

EMBED_INPUT_MAX_CHARS = 2_000
SUMMARY_MAX_CHARS = 1_000
BODY_EXCERPT_CHARS = 1_500
EMBED_CONCURRENCY = 2
TRIAGE_CONCURRENCY = 4
# Version label per triage prompt file (``SelectionVariant.triage_prompt``); the
# content hash is appended, so an edited file never reuses a label.
TRIAGE_PROMPT_BASE_VERSIONS: Mapping[str, str] = {
    "triage_v2": "triage-v2",
    "triage": "triage-v3",
    "triage_p0b": "triage-v4",
}
EMBED_TRANSLATE_PROMPT_BASE_VERSION = "embed-translate-v1"
# A headline with any CJK character is embedded as written; others are first
# translated to Traditional Chinese. Comparing 2026-10-03..14 offline, English
# reports found their Chinese event in the top 10 candidates 97% of the time this
# way (with text-embedding-3-large) against 86% embedded in English (3-small).
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
SCHEMA_INVALID = "triage_schema_invalid"
# Feeds that update once a day hand over yesterday's stories hours late. An
# article published this long before its window opened may join an event of the
# window but never opens one, so old news is not selected again as a new story.
STALE_BEFORE_WINDOW = timedelta(hours=2)
# The oldnews variant's guard against recaps and reposts that carry a fresh
# publication date: an article whose core fact (triage's ``event_date``) is
# dated more than this before its window opened is held back the same way.
# A day covers overseas news landing a calendar day late in Taipei.
EVENT_DATE_GRACE = timedelta(days=1)

# Triage waits until the body fetch has a verdict (spec §5.1); a failed fetch
# triages on title and summary alone.
TRIAGE_READY = NewsroomArticle.fetch_status.in_(("done", "failed"))


@dataclass(frozen=True, slots=True)
class TriagePrompt:
    text: str
    version: str


def _prompt_text(name: str) -> str:
    return (
        files("daily_insights_api.modules.newsroom")
        .joinpath("prompts", f"{name}.txt")
        .read_text(encoding="utf-8")
        .strip()
    )


def _versioned_prompt(name: str, base_version: str, addenda: tuple[str, ...] = ()) -> TriagePrompt:
    """``prompts/<name>.txt`` with each addendum appended after a blank line.

    The label names the addenda (``triage-v4+triage_addendum_event_date``) and
    ends with a hash of the whole text, so an edited file never reuses a label.
    """
    text = "\n\n".join(_prompt_text(part) for part in (name, *addenda))
    digest = hashlib.sha256(text.encode()).hexdigest()
    label = "+".join((base_version, *addenda))
    return TriagePrompt(text=text, version=f"{label}:{digest[:12]}")


def load_triage_prompt(*, variant: SelectionVariant) -> TriagePrompt:
    """The triage prompt ``variant`` is built with (v2 baseline, v3 p0, v4 p0b, plus addenda)."""
    return _triage_prompt(variant.triage_prompt, variant.triage_prompt_addenda)


@cache
def _triage_prompt(name: str, addenda: tuple[str, ...]) -> TriagePrompt:
    return _versioned_prompt(name, TRIAGE_PROMPT_BASE_VERSIONS[name], addenda)


@cache
def load_embed_translate_prompt() -> TriagePrompt:
    return _versioned_prompt("embed_translate", EMBED_TRANSLATE_PROMPT_BASE_VERSION)


def needs_translation(title: str) -> bool:
    """A headline without any CJK character is translated before it is embedded."""
    return _CJK.search(title) is None


def _squash(text: str | None) -> str:
    return " ".join((text or "").split())


def embedding_input(title: str, feed_summary: str | None) -> str:
    """Title and feed summary only, so embedding never waits for the body fetch."""
    parts = [part for part in (_squash(title), _squash(feed_summary)) if part]
    return "\n".join(parts)[:EMBED_INPUT_MAX_CHARS]


def triage_payload(
    *,
    title: str,
    source_name: str,
    trust_tier: int,
    summary: str | None,
    body_excerpt: str | None,
    candidates: Sequence[CandidateEvent],
    published_at: datetime | None = None,
    with_published_at: bool = False,
) -> dict[str, Any]:
    """The article and its candidate events as triage sees them.

    ``with_published_at`` (the event-date prompt) adds the publisher's date in
    Taipei time, so relative dates in the text can be resolved.
    """
    article: dict[str, Any] = {
        "title": _squash(title),
        "source": {"name": source_name, "trust_tier": trust_tier},
        "summary": _squash(summary)[:SUMMARY_MAX_CHARS],
    }
    if with_published_at:
        article["published_at"] = (
            None if published_at is None else published_at.astimezone(clock.TAIPEI).isoformat()
        )
    if body_excerpt:
        article["body_excerpt"] = body_excerpt[:BODY_EXCERPT_CHARS].strip()
    return {
        "article": article,
        "candidate_events": [
            {
                "id": str(candidate.id),
                "working_title": candidate.working_title,
                "titles": list(candidate.titles),
            }
            for candidate in candidates
        ],
    }


def published_before_window(published_at: datetime | None, edition_date: date) -> bool:
    """Whether the publisher dated the article well before its edition window opened."""
    if published_at is None:
        return False
    return published_at < clock.window(edition_date)[0] - STALE_BEFORE_WINDOW


def dated_before_window(result: TriageResult, edition_date: date) -> bool:
    """Whether triage dated the core fact more than ``EVENT_DATE_GRACE`` before the window.

    Edition D's window opens on D-1 (Taipei), so for D = 10-12 an event date of
    10-09 or earlier is old and 10-10 is not. A preview is about a later date
    and an unknown date proves nothing, so neither is ever old.
    """
    if result.event_date is None or result.story_type == "preview":
        return False
    opens_on = clock.window(edition_date)[0].astimezone(clock.TAIPEI).date()
    return result.event_date < opens_on - EVENT_DATE_GRACE


def judged_result(result: TriageResult, *, dated_old: bool) -> TriageResult:
    """The judgements scores are computed from: an old event date has no new facts.

    Nor is it a clear escalation, whose waiver would cancel the missing new facts.
    """
    if not dated_old:
        return result
    return result.model_copy(
        update={
            "new_facts": False,
            "escalation": min(result.escalation, scoring.CLEAR_ESCALATION - 1),
        }
    )


def resolve_event_choice(
    result: TriageResult, candidates: Sequence[CandidateEvent], *, relevant: bool
) -> uuid.UUID | EventNew | None:
    """The candidate the model matched, a new event to open, or None when irrelevant.

    A relevant article without an event, or a match outside the offered
    candidates, is a schema violation and retried like any invalid output.
    """
    if not relevant:
        return None
    if isinstance(result.event, EventNew):
        return result.event
    if isinstance(result.event, EventMatch):
        wanted = result.event.match.strip().lower()
        for candidate in candidates:
            if str(candidate.id) == wanted:
                return candidate.id
    raise RetryableStageError(SCHEMA_INVALID)


def _mark_call_audit(
    database: AsyncSession, error: RetryableStageError, subject: uuid.UUID
) -> None:
    """Mark the successful call's audit row as the failure; ``run_claimed`` keeps it."""
    for row in database.new:
        if isinstance(row, NewsroomLlmCall) and row.subject_id == subject:
            row.error_code = error.code


async def assign_event(
    database: AsyncSession,
    *,
    article_id: uuid.UUID,
    edition_date: date,
    embedding: Sequence[float],
    choice: uuid.UUID | EventNew,
    known_event_ids: Collection[uuid.UUID],
    may_open: bool = True,
) -> uuid.UUID | None:
    """Write-side of event assignment, serialised per window.

    A matched event that was merged meanwhile resolves to its merge target. A new
    event first re-checks the window under the lock against events opened while
    the model call was in flight, so a story opened by a concurrent article is
    joined instead of duplicated. ``known_event_ids`` (the window's open events
    before the call, plus the candidates offered) are skipped: the model already
    judged those, and the re-check only guards against concurrency.

    With ``may_open`` false the article joins an event or stays out of the
    window's events (``None``): it never opens one.
    """
    await clustering.lock_window(database, edition_date)
    if isinstance(choice, uuid.UUID):
        resolved = await clustering.resolve_open_event(database, choice)
        if resolved is None:
            raise RetryableStageError("triage_event_unavailable")
        return resolved
    nearest = await clustering.nearest_events(
        database,
        edition_date=edition_date,
        embedding=embedding,
        exclude_article_id=article_id,
        exclude_event_ids=known_event_ids,
        limit=1,
        with_titles=False,
    )
    if nearest and nearest[0].similarity >= clustering.SAME_EVENT_SIMILARITY:
        return nearest[0].id
    if not may_open:
        return None
    event = NewsroomEvent(
        edition_date=edition_date,
        working_title=choice.new,
        status="open",
        created_by="triage",
    )
    database.add(event)
    await database.flush()
    return event.id


async def translated_embedding_input(
    runtime: Runtime,
    database: AsyncSession,
    *,
    article_id: uuid.UUID,
    title: str,
    feed_summary: str | None,
    last_attempt: bool,
) -> str:
    """The Traditional Chinese embedding input for a non-Chinese article.

    A failed translation is retried with the stage; on its last attempt, or on
    a fatal provider error, the article is embedded as written rather than never
    reaching triage.
    """
    prompt = load_embed_translate_prompt()
    try:
        result = await runtime.llm.complete(
            database,
            model=runtime.settings.newsroom_translate_model,
            system=prompt.text,
            payload={"title": _squash(title), "summary": _squash(feed_summary)[:SUMMARY_MAX_CHARS]},
            result_type=EmbeddingTranslation,
            audit=CallAudit(stage="embed", subject_id=article_id, prompt_version=prompt.version),
        )
    except (RetryableStageError, FatalStageError) as error:
        if isinstance(error, RetryableStageError) and not last_attempt:
            raise
        database.add_all(error.audit_rows)
        logger.warning(
            "newsroom.embed_translation_skipped",
            extra={"article_id": str(article_id), "error_code": error.code},
        )
        return embedding_input(title, feed_summary)
    return embedding_input(result.title, result.summary) or embedding_input(title, feed_summary)


def _embed_handler(runtime: Runtime) -> StageHandler:
    async def handle(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
        title, feed_summary = (
            await database.execute(
                select(NewsroomArticle.title, NewsroomArticle.feed_summary).where(
                    NewsroomArticle.id == claim_.row_id
                )
            )
        ).one()
        text = embedding_input(title, feed_summary)
        if not text:
            return {"embed_status": "failed", "embed_error_code": "embed_input_empty"}
        if needs_translation(title):
            text = await translated_embedding_input(
                runtime,
                database,
                article_id=claim_.row_id,
                title=title,
                feed_summary=feed_summary,
                last_attempt=claim_.attempt >= queue.EMBED.max_attempts,
            )
        vectors = await runtime.embedder.embed(
            database, [text], audit=CallAudit(stage="embed", subject_id=claim_.row_id)
        )
        return {
            "embedding": vectors[0],
            "embedding_text": text,
            "triage_status": "pending",
            "triage_attempts": 0,
            "triage_next_attempt_at": None,
            "triage_error_code": None,
        }

    return handle


def _triage_handler(runtime: Runtime) -> StageHandler:
    async def handle(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
        variant = get_variant(runtime.settings.newsroom_selection_variant)
        article = (
            await database.execute(
                select(
                    NewsroomArticle.title,
                    NewsroomArticle.feed_summary,
                    NewsroomArticle.body_status,
                    func.left(NewsroomArticle.body, BODY_EXCERPT_CHARS).label("body_excerpt"),
                    NewsroomArticle.edition_date,
                    NewsroomArticle.published_at,
                    NewsroomArticle.embedding,
                    NewsroomSource.name.label("source_name"),
                    NewsroomSource.kind.label("source_kind"),
                    NewsroomSource.trust_tier,
                )
                .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
                .where(NewsroomArticle.id == claim_.row_id)
            )
        ).one()
        if article.embedding is None:
            return {"triage_status": "failed", "triage_error_code": "triage_embedding_missing"}
        embedding = [float(value) for value in article.embedding]
        # Snapshot before the search: anything not in it or among the candidates
        # was opened after the model saw the window.
        known_event_ids = await clustering.open_event_ids(database, article.edition_date)
        candidates = await clustering.nearest_events(
            database,
            edition_date=article.edition_date,
            embedding=embedding,
            exclude_article_id=claim_.row_id,
        )
        prompt = load_triage_prompt(variant=variant)
        result = await runtime.llm.complete(
            database,
            model=runtime.settings.newsroom_triage_model,
            system=prompt.text,
            payload=triage_payload(
                title=article.title,
                source_name=article.source_name,
                trust_tier=article.trust_tier,
                summary=article.feed_summary,
                body_excerpt=article.body_excerpt if article.body_status == "ok" else None,
                candidates=candidates,
                published_at=article.published_at,
                with_published_at=variant.event_date_rule,
            ),
            result_type=TriageResult,
            audit=CallAudit(
                stage="triage", subject_id=claim_.row_id, prompt_version=prompt.version
            ),
        )
        # An admin files a manual URL under the edition they are reviewing, so
        # neither its publication date nor its event date ever holds it back.
        manual = article.source_kind == MANUAL_KIND
        stale = not manual and published_before_window(article.published_at, article.edition_date)
        dated_old = (
            variant.event_date_rule
            and not manual
            and dated_before_window(result, article.edition_date)
        )
        scores = scoring.market_scores(judged_result(result, dated_old=dated_old), variant=variant)
        relevant = scoring.is_relevant(scores)
        try:
            choice = resolve_event_choice(result, candidates, relevant=relevant)
        except RetryableStageError as error:
            _mark_call_audit(database, error, claim_.row_id)
            raise
        event_id = (
            None
            if choice is None
            else await assign_event(
                database,
                article_id=claim_.row_id,
                edition_date=article.edition_date,
                embedding=embedding,
                choice=choice,
                known_event_ids=known_event_ids | {candidate.id for candidate in candidates},
                may_open=not (stale or dated_old),
            )
        )
        signals = scoring.triage_signals(result) | {"stale": stale}
        if variant.event_date_rule:
            signals["dated_old"] = dated_old
        return {
            "relevant": relevant,
            "topic": result.topic,
            "story_type": result.story_type,
            "triage_signals": signals,
            "market_scores": scores,
            "event_id": event_id,
            "triaged_at": datetime.now(UTC),
        }

    return handle


def register(runtime: Runtime) -> Registration:
    """Stage bindings for ``queue.EMBED`` and ``queue.TRIAGE``."""
    return Registration(
        stages=[
            StageBinding(queue.EMBED, _embed_handler(runtime), concurrency=EMBED_CONCURRENCY),
            StageBinding(
                queue.TRIAGE,
                _triage_handler(runtime),
                concurrency=TRIAGE_CONCURRENCY,
                extra_filter=TRIAGE_READY,
            ),
        ]
    )
