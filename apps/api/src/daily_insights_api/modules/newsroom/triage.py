"""Workstream ②: embedding and per-article triage with event assignment (spec §6.2).

Owner: triage worktree. EMBED turns title + feed summary into a vector and hands
the article to TRIAGE (§5.1). TRIAGE scores the article, then attaches it to an
event of its edition window; the model call runs concurrently, the event write
is serialised per window by ``clustering.lock_window``.
"""

import hashlib
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import cache
from importlib.resources import files
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom import clock, clustering, queue, scoring
from daily_insights_api.modules.newsroom.clustering import CandidateEvent
from daily_insights_api.modules.newsroom.contracts import EventMatch, EventNew, TriageResult
from daily_insights_api.modules.newsroom.ingestion.allowlist import MANUAL_KIND
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEvent,
    NewsroomLlmCall,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.queue import Claim, RetryableStageError, StageHandler
from daily_insights_api.modules.newsroom.worker import Registration, Runtime, StageBinding

EMBED_INPUT_MAX_CHARS = 2_000
SUMMARY_MAX_CHARS = 1_000
BODY_EXCERPT_CHARS = 1_500
EMBED_CONCURRENCY = 2
TRIAGE_CONCURRENCY = 4
TRIAGE_PROMPT_BASE_VERSION = "triage-v2"
SCHEMA_INVALID = "triage_schema_invalid"
# Feeds that update once a day hand over yesterday's stories hours late. An
# article published this long before its window opened may join an event of the
# window but never opens one, so old news is not selected again as a new story.
STALE_BEFORE_WINDOW = timedelta(hours=2)

# Triage waits until the body fetch has a verdict (spec §5.1); a failed fetch
# triages on title and summary alone.
TRIAGE_READY = NewsroomArticle.fetch_status.in_(("done", "failed"))


@dataclass(frozen=True, slots=True)
class TriagePrompt:
    text: str
    version: str


@cache
def load_triage_prompt() -> TriagePrompt:
    text = (
        files("daily_insights_api.modules.newsroom")
        .joinpath("prompts", "triage.txt")
        .read_text(encoding="utf-8")
        .strip()
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    return TriagePrompt(text=text, version=f"{TRIAGE_PROMPT_BASE_VERSION}:{digest[:12]}")


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
) -> dict[str, Any]:
    article: dict[str, Any] = {
        "title": _squash(title),
        "source": {"name": source_name, "trust_tier": trust_tier},
        "summary": _squash(summary)[:SUMMARY_MAX_CHARS],
    }
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


def _keep_call_audit(
    database: AsyncSession, error: RetryableStageError, subject: uuid.UUID
) -> None:
    """Carry the successful call's audit row across the rollback, marked as the failure."""
    for row in list(database.new):
        if isinstance(row, NewsroomLlmCall) and row.subject_id == subject:
            row.error_code = error.code
            error.audit_rows.append(row)


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
        vectors = await runtime.embedder.embed(
            database, [text], audit=CallAudit(stage="embed", subject_id=claim_.row_id)
        )
        return {
            "embedding": vectors[0],
            "triage_status": "pending",
            "triage_attempts": 0,
            "triage_next_attempt_at": None,
            "triage_error_code": None,
        }

    return handle


def _triage_handler(runtime: Runtime) -> StageHandler:
    async def handle(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
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
        prompt = load_triage_prompt()
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
            ),
            result_type=TriageResult,
            audit=CallAudit(
                stage="triage", subject_id=claim_.row_id, prompt_version=prompt.version
            ),
        )
        scores = scoring.market_scores(result)
        relevant = scoring.is_relevant(scores)
        try:
            choice = resolve_event_choice(result, candidates, relevant=relevant)
        except RetryableStageError as error:
            _keep_call_audit(database, error, claim_.row_id)
            raise
        # An admin files a manual URL under the edition they are reviewing, so
        # its publication date never holds it back.
        stale = article.source_kind != MANUAL_KIND and published_before_window(
            article.published_at, article.edition_date
        )
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
                may_open=not stale,
            )
        )
        return {
            "relevant": relevant,
            "topic": result.topic,
            "story_type": result.story_type,
            "triage_signals": scoring.triage_signals(result) | {"stale": stale},
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
