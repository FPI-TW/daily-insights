"""Workstream ③: 08:00 edition assembly — scoring, editor pass, quotas (spec §6.3).

Owner: editions worktree. Triggered by the ``newsroom_assemble`` orchestration
function; must be idempotent per edition date.

Assembly is not a queue stage, so the editor call gets a small bounded retry
here, using the same retryable/fatal split as the queue (spec §5). Exhausted
retries fall back to score order (D5).
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom import clock, queue
from daily_insights_api.modules.newsroom.contracts import EditorResult
from daily_insights_api.modules.newsroom.models import (
    MARKET_CODES,
    NewsroomArticle,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEvent,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.notifier import Notice
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.queue import FatalStageError, RetryableStageError
from daily_insights_api.modules.newsroom.translation import contract_error, load_prompt
from daily_insights_api.modules.newsroom.worker import Runtime

logger = logging.getLogger(__name__)

EDITOR_PROMPT_VERSION = "newsroom.editor.v1"
CANDIDATE_LIMIT = 30
HEADLINES_PER_EVENT = 5
MULTI_SOURCE_BONUS = 5.0
MULTI_SOURCE_BONUS_CAP = 20.0
FIVE_STAR_UNLIMITED = 5
FOUR_STAR_LIMIT = 5
FILL_TARGET = 5
FALLBACK_LIMIT = 5
EDITOR_ATTEMPTS = 3
EDITOR_RETRY_DELAY = timedelta(seconds=5)
TRIAGE_POLL = timedelta(seconds=15)
ADMIN_REVIEW_PATH = "/admin/newsroom"

Sleep = Callable[[float], Awaitable[None]]


# --- Pure scoring and quota rules -------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArticleSignal:
    """What one relevant article contributes to its event's score in one market."""

    source_id: uuid.UUID
    market_score: float
    weight: float


def event_score(signals: Sequence[ArticleSignal]) -> float:
    """``max(score * source weight)`` plus 5 per extra distinct source, capped at +20."""
    if not signals:
        return 0.0
    best = max(signal.market_score * signal.weight for signal in signals)
    extra_sources = len({signal.source_id for signal in signals}) - 1
    return best + min(extra_sources * MULTI_SOURCE_BONUS, MULTI_SOURCE_BONUS_CAP)


@dataclass(frozen=True, slots=True)
class Rated:
    event_id: uuid.UUID
    stars: int
    score: float


def _quota_order(rated: Rated) -> tuple[int, float, str]:
    return (-rated.stars, -rated.score, str(rated.event_id))


def apply_quota(rated: Sequence[Rated]) -> list[Rated]:
    """D8 quotas, applied by code rather than the model.

    Every 5-star event is kept; at most five 4-star events; only when 5 + 4
    stars total fewer than five are 1-3 star events used, filling up to five
    items (so at most five fillers). Ordered by stars, then score, descending.
    """
    ordered = sorted(rated, key=_quota_order)
    five = [item for item in ordered if item.stars == 5]
    four = [item for item in ordered if item.stars == 4][:FOUR_STAR_LIMIT]
    chosen = five + four
    if len(chosen) < FILL_TARGET:
        fillers = [item for item in ordered if item.stars <= 3]
        chosen += fillers[: FILL_TARGET - len(chosen)]
    return sorted(chosen, key=_quota_order)


def fallback_selection(scores: Sequence[tuple[uuid.UUID, float]]) -> list[tuple[uuid.UUID, float]]:
    """Editor unavailable: the top five events by score (D5)."""
    return sorted(scores, key=lambda pair: (-pair[1], str(pair[0])))[:FALLBACK_LIMIT]


class EditorContractError(RetryableStageError):
    """The model rated unknown events, skipped some, or rated one twice."""


def validate_ratings(result: EditorResult, expected: set[str]) -> dict[str, int]:
    """Every input event rated exactly once, nothing else (a schema error otherwise)."""
    ratings: dict[str, int] = {}
    for rating in result.ratings:
        if rating.event_id not in expected or rating.event_id in ratings:
            raise EditorContractError("editor_schema_invalid")
        ratings[rating.event_id] = rating.stars
    if ratings.keys() != expected:
        raise EditorContractError("editor_schema_invalid")
    return ratings


# --- Candidate loading -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    event_id: uuid.UUID
    working_title: str
    score: float
    headlines: tuple[tuple[str, str], ...]
    article_count: int
    source_count: int


@dataclass(frozen=True, slots=True)
class _ArticleRow:
    event_id: uuid.UUID
    title: str
    source_id: uuid.UUID
    source_name: str
    weight: float
    trust_tier: int
    market_scores: dict[str, Any]
    has_body: bool


async def _load_event_articles(
    database: AsyncSession, edition_date: date
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, list[_ArticleRow]]]:
    rows = (
        await database.execute(
            select(
                NewsroomEvent.id,
                NewsroomEvent.working_title,
                NewsroomArticle.title,
                NewsroomSource.id,
                NewsroomSource.name,
                NewsroomSource.weight,
                NewsroomSource.trust_tier,
                NewsroomArticle.market_scores,
                NewsroomArticle.body_status,
            )
            .join(NewsroomArticle, NewsroomArticle.event_id == NewsroomEvent.id)
            .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
            .where(
                NewsroomEvent.edition_date == edition_date,
                NewsroomEvent.status == "open",
                NewsroomArticle.relevant.is_(True),
            )
            .order_by(NewsroomEvent.id, NewsroomArticle.first_seen_at, NewsroomArticle.id)
        )
    ).all()
    titles: dict[uuid.UUID, str] = {}
    articles: dict[uuid.UUID, list[_ArticleRow]] = {}
    for event_id, working_title, title, source_id, name, weight, tier, scores, body in rows:
        titles[event_id] = working_title
        articles.setdefault(event_id, []).append(
            _ArticleRow(
                event_id=event_id,
                title=title,
                source_id=source_id,
                source_name=name,
                weight=float(weight),
                trust_tier=int(tier),
                market_scores=scores or {},
                has_body=body == "ok",
            )
        )
    return titles, articles


def _market_score(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def rank_candidates(
    titles: dict[uuid.UUID, str], articles: dict[uuid.UUID, list[_ArticleRow]], market: str
) -> list[Candidate]:
    """Eligible events for ``market`` by score: relevant, scored, with a full text."""
    candidates: list[Candidate] = []
    for event_id, rows in articles.items():
        if not any(row.has_body for row in rows):
            continue
        signals = [
            ArticleSignal(row.source_id, _market_score(row.market_scores.get(market)), row.weight)
            for row in rows
        ]
        if max(signal.market_score for signal in signals) <= 0:
            continue
        best_first = sorted(rows, key=lambda row: (-row.trust_tier, -row.weight))
        candidates.append(
            Candidate(
                event_id=event_id,
                working_title=titles[event_id],
                score=event_score(signals),
                headlines=tuple(
                    (row.title, row.source_name) for row in best_first[:HEADLINES_PER_EVENT]
                ),
                article_count=len(rows),
                source_count=len({row.source_id for row in rows}),
            )
        )
    candidates.sort(key=lambda item: (-item.score, str(item.event_id)))
    return candidates[:CANDIDATE_LIMIT]


# --- Editor pass -----------------------------------------------------------------


def _editor_payload(market: str, candidates: Sequence[Candidate]) -> dict[str, Any]:
    return {
        "market": market,
        "events": [
            {
                "event_id": str(candidate.event_id),
                "working_title": candidate.working_title,
                "report_count": candidate.article_count,
                "source_count": candidate.source_count,
                "headlines": [
                    {"title": title, "source": source} for title, source in candidate.headlines
                ],
            }
            for candidate in candidates
        ],
    }


async def _rate(
    runtime: Runtime,
    market: str,
    candidates: Sequence[Candidate],
    *,
    sleep: Sleep,
    retry_delay: timedelta,
) -> dict[str, int] | None:
    """Editor stars per event id, or ``None`` once retries are exhausted."""
    expected = {str(candidate.event_id) for candidate in candidates}
    for attempt in range(1, EDITOR_ATTEMPTS + 1):
        async with runtime.session_factory() as database:
            try:
                result = await runtime.llm.complete(
                    database,
                    model=runtime.settings.newsroom_editor_model,
                    system=load_prompt("editor"),
                    payload=_editor_payload(market, candidates),
                    result_type=EditorResult,
                    audit=CallAudit("editor", None, EDITOR_PROMPT_VERSION),
                )
                ratings = validate_ratings(result, expected)
            except (RetryableStageError, FatalStageError) as error:
                if isinstance(error, EditorContractError):
                    error.audit_rows.extend(contract_error(database, error.code).audit_rows)
                await database.rollback()
                database.add_all(error.audit_rows)
                await database.commit()
                logger.warning(
                    "newsroom.editor_failed",
                    extra={"market": market, "attempt": attempt, "code": error.code},
                )
                if isinstance(error, FatalStageError) or attempt == EDITOR_ATTEMPTS:
                    return None
                await sleep(retry_delay.total_seconds())
                continue
            await database.commit()
            return ratings
    return None


# --- Writing the draft -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pick:
    event_id: uuid.UUID
    stars: int | None
    score: float


@dataclass
class MarketReport:
    market: str
    action: str  # assembled | skipped_published
    selection_mode: str | None = None
    item_count: int = 0
    five_star_titles: list[str] = field(default_factory=list)


@dataclass
class AssemblyReport:
    edition_date: date
    ignored_pending_triage: int
    markets: list[MarketReport] = field(default_factory=list)

    @property
    def assembled(self) -> list[MarketReport]:
        return [market for market in self.markets if market.action == "assembled"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "edition_date": self.edition_date.isoformat(),
            "ignored_pending_triage": self.ignored_pending_triage,
            "markets": [
                {
                    "market": market.market,
                    "action": market.action,
                    "selection_mode": market.selection_mode,
                    "item_count": market.item_count,
                }
                for market in self.markets
            ],
        }


async def _locked_edition(
    database: AsyncSession, edition_date: date, market: str
) -> NewsroomEdition:
    await database.execute(
        insert(NewsroomEdition)
        .values(
            id=uuid.uuid4(),
            edition_date=edition_date,
            market_code=market,
            auto_publish_at=clock.auto_publish_at(edition_date),
            late_fill_deadline=clock.late_fill_deadline(edition_date),
        )
        .on_conflict_do_nothing(constraint="uq_newsroom_edition_date_market")
    )
    edition = (
        await database.scalars(
            select(NewsroomEdition)
            .where(
                NewsroomEdition.edition_date == edition_date,
                NewsroomEdition.market_code == market,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one()
    return edition


async def write_draft(
    database: AsyncSession,
    *,
    edition_date: date,
    market: str,
    picks: Sequence[Pick],
    selection_mode: str,
    ignored_pending_triage: int,
    now: datetime,
) -> NewsroomEdition | None:
    """Create or rebuild the market's draft; ``None`` when it is already published.

    A rebuild keeps admin decisions: manual items stay, and removed items stay
    removed. Model items the editor no longer picks are dropped. Model picks
    take ranks 1..n in quota order, followed by manual items in their previous
    order, then removed items.
    """
    edition = await _locked_edition(database, edition_date, market)
    if edition.status == "published":
        return None
    existing = {
        item.event_id: item
        for item in (
            await database.scalars(
                select(NewsroomEditionItem).where(NewsroomEditionItem.edition_id == edition.id)
            )
        ).all()
    }
    picked_ids = {pick.event_id for pick in picks}
    ordered: list[NewsroomEditionItem] = []
    for pick in picks:
        item = existing.get(pick.event_id)
        if item is None:
            item = NewsroomEditionItem(
                edition_id=edition.id,
                event_id=pick.event_id,
                rank=1,
                origin="model",
                stars=pick.stars,
            )
            database.add(item)
        elif item.origin != "manual":
            item.stars = pick.stars
        item.editor_score = pick.score
        ordered.append(item)
    leftovers = sorted(
        (item for event_id, item in existing.items() if event_id not in picked_ids),
        key=lambda item: item.rank,
    )
    for item in leftovers:
        if item.origin == "manual" or item.removed_at is not None:
            ordered.append(item)
        else:
            await database.delete(item)
    ordered.sort(key=lambda item: item.removed_at is not None)
    for rank, item in enumerate(ordered, start=1):
        item.rank = rank
    edition.selection_mode = selection_mode
    edition.assembled_at = now
    edition.ignored_pending_triage = ignored_pending_triage
    await database.flush()
    await _queue_analysis(database, [item.event_id for item in ordered if item.removed_at is None])
    return edition


async def _queue_analysis(database: AsyncSession, event_ids: Sequence[uuid.UUID]) -> None:
    """Selected events go to analysis unless already analysed or already queued."""
    if not event_ids:
        return
    due = (
        await database.scalars(
            select(NewsroomEvent.id).where(
                NewsroomEvent.id.in_(list(event_ids)),
                NewsroomEvent.analysis_status.in_(("idle", "failed", "needs_body")),
            )
        )
    ).all()
    await queue.enqueue(database, queue.ANALYSIS, list(due))


# --- Orchestration entry point ---------------------------------------------------


async def _untriaged_count(database: AsyncSession, edition_date: date) -> int:
    """Window articles whose triage has not settled (waiting on embed or triage)."""
    count = await database.scalar(
        select(func.count())
        .select_from(NewsroomArticle)
        .where(
            NewsroomArticle.edition_date == edition_date,
            or_(
                NewsroomArticle.triage_status == "pending",
                NewsroomArticle.embed_status == "pending",
            ),
        )
    )
    return int(count or 0)


async def wait_for_triage(
    runtime: Runtime,
    edition_date: date,
    *,
    now: Callable[[], datetime],
    sleep: Sleep,
    poll: timedelta = TRIAGE_POLL,
) -> int:
    """Wait until 08:10 for window triage to settle; returns what is still pending."""
    deadline = clock.window(edition_date)[1] + clock.TRIAGE_GRACE
    while True:
        async with runtime.session_factory() as database:
            pending = await _untriaged_count(database, edition_date)
        remaining = (deadline - now()).total_seconds()
        if pending == 0 or remaining <= 0:
            return pending
        await sleep(min(poll.total_seconds(), remaining))


def review_link(runtime: Runtime, edition_date: date) -> str:
    base = runtime.settings.newsroom_admin_base_url.rstrip("/")
    return f"{base}{ADMIN_REVIEW_PATH}?date={edition_date.isoformat()}"


async def _assemble_market(
    runtime: Runtime,
    edition_date: date,
    market: str,
    titles: dict[uuid.UUID, str],
    articles: dict[uuid.UUID, list[_ArticleRow]],
    *,
    ignored: int,
    now: Callable[[], datetime],
    sleep: Sleep,
    retry_delay: timedelta,
) -> MarketReport:
    async with runtime.session_factory() as database:
        published = await database.scalar(
            select(NewsroomEdition.id).where(
                NewsroomEdition.edition_date == edition_date,
                NewsroomEdition.market_code == market,
                NewsroomEdition.status == "published",
            )
        )
    if published is not None:
        return MarketReport(market, "skipped_published")
    candidates = rank_candidates(titles, articles, market)
    by_id = {candidate.event_id: candidate for candidate in candidates}
    ratings = (
        await _rate(runtime, market, candidates, sleep=sleep, retry_delay=retry_delay)
        if candidates
        else {}
    )
    if ratings is None:
        mode = "fallback"
        picks = [
            Pick(event_id, None, score)
            for event_id, score in fallback_selection(
                [(candidate.event_id, candidate.score) for candidate in candidates]
            )
        ]
    else:
        mode = "editor"
        picks = [
            Pick(item.event_id, item.stars, item.score)
            for item in apply_quota(
                [
                    Rated(candidate.event_id, ratings[str(candidate.event_id)], candidate.score)
                    for candidate in candidates
                ]
            )
        ]
    async with runtime.session_factory() as database:
        edition = await write_draft(
            database,
            edition_date=edition_date,
            market=market,
            picks=picks,
            selection_mode=mode,
            ignored_pending_triage=ignored,
            now=now(),
        )
        await database.commit()
    if edition is None:
        return MarketReport(market, "skipped_published")
    if mode == "fallback":
        await runtime.notifier.send(
            Notice(
                kind="selection_fallback",
                title=f"{edition_date.isoformat()} {market} 精選失敗 已改用粗分排序",
                lines=(f"入選 {len(picks)} 則 星等留空",),
                link=review_link(runtime, edition_date),
            )
        )
    return MarketReport(
        market,
        "assembled",
        selection_mode=mode,
        item_count=len(picks),
        five_star_titles=[by_id[pick.event_id].working_title for pick in picks if pick.stars == 5],
    )


async def assemble_editions(
    runtime: Runtime,
    edition_date: date,
    *,
    now: Callable[[], datetime] | None = None,
    sleep: Sleep = asyncio.sleep,
    retry_delay: timedelta = EDITOR_RETRY_DELAY,
) -> AssemblyReport:
    """Build (or rebuild) every market's draft for ``edition_date`` (spec §6.3 1-7)."""
    clock_now = now or (lambda: datetime.now(UTC))
    ignored = await wait_for_triage(runtime, edition_date, now=clock_now, sleep=sleep)
    if ignored:
        logger.warning(
            "newsroom.assembly_ignored_untriaged",
            extra={"edition_date": edition_date.isoformat(), "count": ignored},
        )
    async with runtime.session_factory() as database:
        titles, articles = await _load_event_articles(database, edition_date)
    report = AssemblyReport(edition_date, ignored)
    for market in MARKET_CODES:
        report.markets.append(
            await _assemble_market(
                runtime,
                edition_date,
                market,
                titles,
                articles,
                ignored=ignored,
                now=clock_now,
                sleep=sleep,
                retry_delay=retry_delay,
            )
        )
    if report.assembled:
        lines = [
            f"{market.market}: {market.item_count} 則"
            + (" (精選失敗 改用粗分排序)" if market.selection_mode == "fallback" else "")
            for market in report.assembled
        ]
        lines += [
            f"5 星 {market.market}: {title}"
            for market in report.assembled
            for title in market.five_star_titles
        ]
        if ignored:
            lines.append(f"組稿時仍有 {ignored} 篇文章未完成初篩 已忽略")
        await runtime.notifier.send(
            Notice(
                kind="draft_ready",
                title=f"{edition_date.isoformat()} 重點新聞草稿完成",
                lines=tuple(lines),
                link=review_link(runtime, edition_date),
            )
        )
    return report
