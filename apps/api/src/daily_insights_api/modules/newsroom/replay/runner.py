"""Run a replay of a prepared range: ``score``, ``editor`` or ``triage``.

Every run works in its own copy of the range's template database (see
``snapshot``) with a ``Runtime`` built here: settings aimed at that copy, the
chosen variant, no Slack webhook, a notifier that only records, and model
clients that answer from the response cache first.

``score`` calls no model: stored triage judgements are rescored under the
variant, the candidate list is ranked again, and production's editor stars are
reused through the quota (an event that newly enters the top 40 has none and is
shown as unrated). ``editor`` keeps triage and event assignment and runs the
real assembly (duplicate screen, repeat screen, editor pass, quotas).
``triage`` also clears the window's events and triages every article again in
``first_seen_at`` order through the real triage handler before assembling.

Days run in order. After a day is assembled its drafts are marked published in
the copy, so the next day's repeat screen compares with what this replay
selected; days before the range compare with production's editions.
"""

import asyncio
import json
import logging
import os
import subprocess
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import event as orm_event
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.newsroom import assembly, clock, queue, scoring, triage
from daily_insights_api.modules.newsroom.models import (
    MARKET_CODES,
    NewsroomArticle,
    NewsroomDedupeDecision,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEditorRating,
    NewsroomEvent,
)
from daily_insights_api.modules.newsroom.notifier import LogNotifier
from daily_insights_api.modules.newsroom.providers import (
    Embedder,
    JsonModel,
    OpenAICompatibleEmbedder,
    OpenAICompatibleJsonModel,
)
from daily_insights_api.modules.newsroom.queue import Claim
from daily_insights_api.modules.newsroom.replay import pipeline, snapshot
from daily_insights_api.modules.newsroom.replay.cache import (
    CachedEmbedder,
    CachedJsonModel,
    CacheStats,
    ReplayCacheMiss,
    ResponseCache,
)
from daily_insights_api.modules.newsroom.replay.metrics import (
    DEFAULT_TRACKS,
    EventRow,
    MarketDay,
    Track,
    TrackedRow,
    summary,
)
from daily_insights_api.modules.newsroom.replay.snapshot import (
    ReplayEnvironment,
    ReplayError,
    Snapshot,
)
from daily_insights_api.modules.newsroom.replay.tw50 import match_tw50
from daily_insights_api.modules.newsroom.variants import SelectionVariant
from daily_insights_api.modules.newsroom.worker import Runtime

logger = logging.getLogger(__name__)

Stages = Literal["score", "editor", "triage"]
STAGES: tuple[Stages, ...] = ("score", "editor", "triage")
MAX_CONCURRENCY = 8
HEADLINES_SHOWN = 3
SLACK_ENVIRONMENT = "DAILY_INSIGHTS_NEWSROOM_SLACK_WEBHOOK_URL"
REVIEW_BASE_URL = "http://replay.invalid"


@dataclass(frozen=True, slots=True)
class RunOptions:
    snapshot: Snapshot
    variant: SelectionVariant
    stages: Stages
    concurrency: int = 6
    # Fail on a cache miss instead of calling a provider.
    offline: bool = False
    keep_database: bool = False
    tracks: tuple[Track, ...] = DEFAULT_TRACKS


# --- Deterministic triage order --------------------------------------------------------

_member: ContextVar[int | None] = ContextVar("replay_triage_member", default=None)
_article: ContextVar[uuid.UUID | None] = ContextVar("replay_triage_article", default=None)
# Events a triage replay opens are named after the article that opened them.
# Assembly breaks exact ties (articles of one feed poll share ``first_seen_at``)
# by event id, so random ids would reorder the editor's list from run to run.
REPLAY_EVENT_NAMESPACE = uuid.UUID("6f1d3a52-8a8e-4f0b-9a63-6d1e2b7c4e10")


def _replay_event_id(target: NewsroomEvent, args: Any, kwargs: dict[str, Any]) -> None:
    article_id = _article.get()
    if article_id is not None and kwargs.get("id") is None:
        target.id = uuid.uuid5(REPLAY_EVENT_NAMESPACE, str(article_id))


class TriageGate:
    """Concurrent triage that still clusters the same way every time.

    Articles run in ``first_seen_at`` order, ``window`` at a time. Article ``i``
    starts once every article up to ``i - window`` is done, reads the window's
    events, asks the model, and then writes its event only after every earlier
    article has written and articles ``i + 1 .. i + window - 1`` have read. So every article sees
    exactly the events of the articles before ``i - window + 1``, however fast
    each model call returns, and a rerun asks the very same questions.
    """

    def __init__(self, total: int, window: int) -> None:
        self._total = total
        self._window = window
        self._arrived: set[int] = set()
        self._finished: set[int] = set()
        # Articles 0 .. done - 1 have all finished. An article that never asks
        # the model (no embedding) can finish out of turn; order follows this.
        self._done = 0
        self._condition = asyncio.Condition()

    async def wait_start(self, index: int) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._done > index - self._window)

    async def arrive(self) -> None:
        index = _member.get()
        if index is None:
            return
        async with self._condition:
            self._arrived.add(index)
            self._condition.notify_all()

    async def wait_turn(self) -> None:
        index = _member.get()
        if index is None:
            return
        later = range(index + 1, min(index + self._window, self._total))
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._done >= index and all(other in self._arrived for other in later)
            )

    async def finish(self, index: int) -> None:
        async with self._condition:
            self._finished.add(index)
            self._arrived.add(index)
            while self._done in self._finished:
                self._done += 1
            self._condition.notify_all()


# --- Run context -----------------------------------------------------------------------


@dataclass
class _Context:
    environment: ReplayEnvironment
    options: RunOptions
    runtime: Runtime
    engine: AsyncEngine
    reference: dict[str, Any]
    gate_holder: list[TriageGate | None]
    day_notes: dict[str, list[str]] = field(default_factory=dict)

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        return self.runtime.session_factory

    @property
    def variant(self) -> SelectionVariant:
        return self.options.variant

    def note(self, day: date, text_: str) -> None:
        self.day_notes.setdefault(day.isoformat(), []).append(text_)


class _HeldGate:
    """The gate of the triage pass in progress, if any (the cache asks it on every triage call)."""

    def __init__(self, holder: list[TriageGate | None]) -> None:
        self._holder = holder

    async def arrive(self) -> None:
        if self._holder[0] is not None:
            await self._holder[0].arrive()

    async def wait_turn(self) -> None:
        if self._holder[0] is not None:
            await self._holder[0].wait_turn()


async def _no_sleep(_: float) -> None:
    return None


def _provider_setting(environment: ReplayEnvironment, name: str, default: str) -> str:
    try:
        return snapshot.read_provider_setting(environment, name) or default
    except OSError:
        return default


def _settings(environment: ReplayEnvironment, options: RunOptions, database: str) -> Settings:
    defaults = Settings(_env_file=None)
    return Settings(
        _env_file=None,
        environment="development",
        database_url=environment.database_url(database),
        newsroom_enabled=True,
        newsroom_selection_variant=options.variant.name,
        newsroom_llm_base_url=_provider_setting(
            environment, "NEWSROOM_LLM_BASE_URL", defaults.newsroom_llm_base_url
        ),
        newsroom_triage_model=_provider_setting(
            environment, "NEWSROOM_TRIAGE_MODEL", defaults.newsroom_triage_model
        ),
        newsroom_editor_model=_provider_setting(
            environment, "NEWSROOM_EDITOR_MODEL", defaults.newsroom_editor_model
        ),
        newsroom_embedding_base_url=_provider_setting(
            environment, "NEWSROOM_EMBEDDING_BASE_URL", defaults.newsroom_embedding_base_url
        ),
        newsroom_embedding_model=_provider_setting(
            environment, "NEWSROOM_EMBEDDING_MODEL", defaults.newsroom_embedding_model
        ),
        # Keys stay out of the settings object; the live clients read them on a miss.
        newsroom_llm_api_key=None,
        newsroom_embedding_api_key=None,
        newsroom_slack_webhook_url=None,
        newsroom_admin_base_url=REVIEW_BASE_URL,
    )


def _key(environment: ReplayEnvironment, name: str) -> str:
    value = snapshot.read_provider_setting(environment, name)
    if value is None:
        raise ReplayError(f"{environment.keys_container} has no {name}")
    return value


def _runtime(
    environment: ReplayEnvironment,
    options: RunOptions,
    settings: Settings,
    engine: AsyncEngine,
    gate_holder: list[TriageGate | None],
    stats: CacheStats,
) -> Runtime:
    cache = ResponseCache(environment.cache_dir)

    def live_model() -> JsonModel:
        return OpenAICompatibleJsonModel(
            base_url=settings.newsroom_llm_base_url,
            api_key=_key(environment, "NEWSROOM_LLM_API_KEY"),
            timeout_seconds=settings.newsroom_llm_timeout_seconds,
        )

    def live_embedder() -> Embedder:
        return OpenAICompatibleEmbedder(
            base_url=settings.newsroom_embedding_base_url,
            api_key=_key(environment, "NEWSROOM_EMBEDDING_API_KEY"),
            model=settings.newsroom_embedding_model,
            timeout_seconds=settings.newsroom_embedding_timeout_seconds,
        )

    return Runtime(
        settings=settings,
        session_factory=create_session_factory(engine),
        llm=CachedJsonModel(
            cache,
            stats,
            live=None if options.offline else live_model,
            gate=_HeldGate(gate_holder),
        ),
        embedder=CachedEmbedder(
            cache,
            stats,
            model=settings.newsroom_embedding_model,
            live=None if options.offline else live_embedder,
        ),
        notifier=LogNotifier(),
        renderer=None,
    )


# --- Shared per-day steps --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EventInfo:
    impact: int | None
    escalation: int | None
    topic: str | None
    titles: tuple[str, ...]
    prod_event_ids: tuple[str, ...]


async def _rescore(context: _Context, day: date) -> None:
    """Recompute stored triage's market scores and relevance under the variant."""
    changed = incomplete = newly_relevant_unassigned = dropped = 0
    async with context.sessions() as database:
        rows = (
            await database.execute(
                select(
                    NewsroomArticle.id,
                    NewsroomArticle.topic,
                    NewsroomArticle.story_type,
                    NewsroomArticle.triage_signals,
                    NewsroomArticle.market_scores,
                    NewsroomArticle.relevant,
                    NewsroomArticle.event_id,
                ).where(
                    NewsroomArticle.edition_date == day,
                    NewsroomArticle.triage_status == "done",
                )
            )
        ).all()
        for article_id, topic, story_type, signals, stored, was_relevant, event_id in rows:
            result = pipeline.triage_result(topic=topic, story_type=story_type, signals=signals)
            if result is None:
                incomplete += 1
                continue
            scores = pipeline.market_scores(result, variant=context.variant)
            relevant = pipeline.is_relevant(scores, variant=context.variant)
            if scores == stored and relevant == bool(was_relevant):
                continue
            changed += 1
            if relevant and not was_relevant and event_id is None:
                newly_relevant_unassigned += 1
            if was_relevant and not relevant:
                dropped += 1
            await database.execute(
                update(NewsroomArticle)
                .where(NewsroomArticle.id == article_id)
                .values(market_scores=scores, relevant=relevant)
            )
        await database.commit()
    context.note(
        day,
        f"重算分數: {len(rows)} 篇已初篩文章中 {changed} 篇的分數或 relevant 改變"
        f" ({dropped} 篇不再 relevant; {newly_relevant_unassigned} 篇新達 relevant 但沒有事件歸屬, "
        f"需 triage 重跑才會進入事件); {incomplete} 篇缺少可重算的初篩判斷, 沿用已存分數",
    )


async def _event_info(context: _Context, day: date) -> dict[uuid.UUID, _EventInfo]:
    async with context.sessions() as database:
        rows = (
            await database.execute(
                text(
                    "SELECT a.event_id, a.triage_signals, a.topic, a.title, o.event_id "
                    "FROM newsroom_articles a "
                    "LEFT JOIN replay_article_origin o ON o.article_id = a.id "
                    "WHERE a.edition_date = :day AND a.event_id IS NOT NULL AND a.relevant "
                    "ORDER BY a.first_seen_at, a.id"
                ),
                {"day": day},
            )
        ).all()
    grouped: dict[uuid.UUID, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[row[0]].append(row)
    info: dict[uuid.UUID, _EventInfo] = {}
    for event_id, members in grouped.items():
        impacts = [int(m[1]["impact"]) for m in members if m[1] and "impact" in m[1]]
        escalations = [int(m[1].get("escalation", 0)) for m in members if m[1]]
        topics = Counter(m[2] for m in members if m[2])
        origins = Counter(str(m[4]) for m in members if m[4] is not None)
        info[event_id] = _EventInfo(
            impact=max(impacts, default=None),
            escalation=max(escalations, default=None),
            topic=topics.most_common(1)[0][0] if topics else None,
            titles=tuple(dict.fromkeys(m[3] for m in members)),
            prod_event_ids=tuple(event for event, _ in origins.most_common()),
        )
    return info


async def _relevance(context: _Context, day: date) -> tuple[int, int, dict[str, int]]:
    async with context.sessions() as database:
        rows = (
            await database.execute(
                select(NewsroomArticle.relevant, NewsroomArticle.market_scores).where(
                    NewsroomArticle.edition_date == day
                )
            )
        ).all()
    per_market = {
        market: sum(
            1
            for _, scores in rows
            if isinstance((scores or {}).get(market), int | float)
            and scores[market] >= scoring.RELEVANT_SCORE
        )
        for market in MARKET_CODES
    }
    return len(rows), sum(1 for relevant, _ in rows if relevant), per_market


def _row(
    event_id: uuid.UUID,
    title: str,
    info: dict[uuid.UUID, _EventInfo],
    *,
    position: int | None,
    score: float | None,
    stars: int | None,
    picked: bool = False,
    rank: int | None = None,
    follow_up: bool = False,
    headlines: Sequence[str] = (),
) -> EventRow:
    details = info.get(event_id)
    shown = list(headlines) or list(details.titles[:HEADLINES_SHOWN] if details else ())
    return EventRow(
        event_id=str(event_id),
        title=title,
        position=position,
        score=score,
        stars=stars,
        picked=picked,
        rank=rank,
        follow_up=follow_up,
        impact=details.impact if details else None,
        escalation=details.escalation if details else None,
        topic=details.topic if details else None,
        headlines=shown[:HEADLINES_SHOWN],
        prod_event_ids=list(details.prod_event_ids if details else ()) or [str(event_id)],
        tw50=match_tw50(title, *shown),
    )


def _reference_picks(
    context: _Context, day: date, market: str
) -> tuple[list[EventRow], list[dict[str, Any]]]:
    when = day.isoformat()
    ratings = [
        row
        for row in context.reference["editor_ratings"]
        if row["edition_date"] == when and row["market_code"] == market
    ]
    position = {row["event_id"]: row["position"] for row in ratings}
    items = [
        row
        for row in context.reference["items"]
        if row["edition_date"] == when and row["market_code"] == market
    ]
    picks = [
        EventRow(
            event_id=row["event_id"],
            title=row["working_title"],
            position=position.get(row["event_id"]),
            score=row["editor_score"],
            stars=row["stars"],
            picked=True,
            rank=row["rank"],
            prod_event_ids=[row["event_id"]],
            tw50=match_tw50(row["working_title"]),
        )
        for row in items
        if not row["removed"] and not row["hidden"]
    ]
    return picks, ratings


def _tracked(
    context: _Context,
    market: str,
    ranking: Sequence[assembly.Candidate],
    rows: dict[uuid.UUID, EventRow],
    info: dict[uuid.UUID, _EventInfo],
) -> list[TrackedRow]:
    tracked: list[TrackedRow] = []
    for track in context.options.tracks:
        if market not in track.markets:
            continue
        for position, candidate in enumerate(ranking, start=1):
            details = info.get(candidate.event_id)
            texts = [candidate.working_title, *(details.titles if details else ())]
            if not track.matches(texts):
                continue
            row = rows.get(candidate.event_id) or _row(
                candidate.event_id,
                candidate.working_title,
                info,
                position=position,
                score=candidate.score,
                stars=None,
            )
            row.position = position
            tracked.append(TrackedRow(track.label, row))
    return tracked


def _story_groups(decisions: Sequence[dict[str, Any]]) -> dict[uuid.UUID, int]:
    """The story groups an assembly left open, rebuilt from its window decisions."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in decisions:
        groups[row["matched_event_id"]].append(row)
    story_groups: dict[uuid.UUID, int] = {}
    for index, (target, members) in enumerate(sorted(groups.items())):
        remaining = [target] + [row["event_id"] for row in members if row["outcome"] != "merged"]
        if len(remaining) >= 2:
            story_groups.update(
                dict.fromkeys((uuid.UUID(event_id) for event_id in remaining), index)
            )
    return story_groups


# --- score -----------------------------------------------------------------------------


async def _score_day(context: _Context, day: date) -> list[MarketDay]:
    await _rescore(context, day)
    async with context.sessions() as database:
        titles, articles = await assembly._load_event_articles(database, day)
    info = await _event_info(context, day)
    total, relevant, per_market = await _relevance(context, day)
    when = day.isoformat()
    decisions = [
        row for row in context.reference["dedupe_decisions"] if row["edition_date"] == when
    ]
    excluded = {
        uuid.UUID(row["event_id"])
        for row in decisions
        if row["scope"] == "published" and row["outcome"] == "excluded"
    }
    follow_ups = {
        uuid.UUID(row["event_id"])
        for row in decisions
        if row["scope"] == "published" and row["outcome"] == "follow_up"
    }
    story_groups = _story_groups([row for row in decisions if row["scope"] == "window"])
    days: list[MarketDay] = []
    for market in MARKET_CODES:
        reference_picks, ratings = _reference_picks(context, day, market)
        reference_stars = {row["event_id"]: int(row["stars"]) for row in ratings}
        stored = {uuid.UUID(row["event_id"]): int(row["stars"]) for row in ratings}
        candidates = pipeline.rank_candidates(
            titles, articles, market, excluded=excluded, variant=context.variant
        )
        rated = [
            assembly.Rated(
                candidate.event_id, stored[candidate.event_id], candidate.score, position
            )
            for position, candidate in enumerate(candidates)
            if candidate.event_id in stored
        ]
        quota = pipeline.apply_quota(rated, story_groups, variant=context.variant)
        ranks = {item.event_id: rank for rank, item in enumerate(quota, start=1)}
        rows = {
            candidate.event_id: _row(
                candidate.event_id,
                candidate.working_title,
                info,
                position=position,
                score=candidate.score,
                stars=stored.get(candidate.event_id),
                picked=candidate.event_id in ranks,
                rank=ranks.get(candidate.event_id),
                follow_up=candidate.event_id in follow_ups,
                headlines=[title for title, _ in candidate.headlines],
            )
            for position, candidate in enumerate(candidates, start=1)
        }
        ranking = pipeline.rank_candidates(
            titles, articles, market, excluded=excluded, variant=context.variant, limit=False
        )
        days.append(
            MarketDay(
                edition_date=when,
                market=market,
                selection_mode="stored",
                candidates=list(rows.values()),
                picks=[rows[item.event_id] for item in quota],
                tracked=_tracked(context, market, ranking, rows, info),
                reference_picks=reference_picks,
                reference_stars=reference_stars,
                articles=total,
                relevant=relevant,
                market_relevant=per_market[market],
                five_star_review=pipeline.five_star_review(quota, variant=context.variant),
                reproduction=_reproduction(candidates, ratings, quota, context, day, market),
                notes=[
                    f"未評 (新進前 40 但正式沒有星等){len(candidates) - len(rated)} 則"
                    if len(candidates) > len(rated)
                    else "前 40 名都有正式星等"
                ],
            )
        )
    return days


def _reproduction(
    candidates: Sequence[assembly.Candidate],
    ratings: Sequence[dict[str, Any]],
    quota: Sequence[assembly.Rated],
    context: _Context,
    day: date,
    market: str,
) -> dict[str, Any]:
    details: list[str] = []
    mine = [
        (str(c.event_id), position, round(c.score, 6)) for position, c in enumerate(candidates, 1)
    ]
    theirs = [(row["event_id"], row["position"], round(float(row["score"]), 6)) for row in ratings]
    for left, right in zip(mine, theirs, strict=False):
        if left != right:
            details.append(
                f"名次 {right[1]}: 正式 {right[0][:8]} {right[2]}, 重播 {left[0][:8]} {left[2]}"
            )
            break
    if len(mine) != len(theirs):
        details.append(f"候選數 正式 {len(theirs)} 重播 {len(mine)}")
    when = day.isoformat()
    production = [
        row["event_id"]
        for row in context.reference["items"]
        if row["edition_date"] == when and row["market_code"] == market and row["origin"] == "model"
    ]
    picked = [str(item.event_id) for item in quota]
    if picked != production:
        details.append(f"版面 正式 {len(production)} 則、重播 {len(picked)} 則, 順序或內容不同")
    return {
        "candidates": mine == theirs,
        "stars": {row["event_id"] for row in ratings} == {left[0] for left in mine},
        "picks": picked == production,
        "details": details,
    }


# --- editor and triage -----------------------------------------------------------------


async def _assemble_day(context: _Context, day: date) -> list[MarketDay]:
    deadline = clock.window(day)[1] + clock.TRIAGE_GRACE + timedelta(minutes=1)
    report = await assembly.assemble_editions(
        context.runtime,
        day,
        now=lambda: deadline,
        sleep=_no_sleep,
        retry_delay=timedelta(0),
    )
    context.note(
        day,
        f"組稿: 合併重複事件 {report.merged_duplicates} 個、"
        f"排除前版重複 {report.excluded_repeats} 個"
        + (
            f"、忽略未完成初篩 {report.ignored_pending_triage} 篇"
            if report.ignored_pending_triage
            else ""
        ),
    )
    async with context.sessions() as database:
        titles, articles = await assembly._load_event_articles(database, day)
        excluded = set(
            (
                await database.scalars(
                    select(NewsroomDedupeDecision.event_id).where(
                        NewsroomDedupeDecision.edition_date == day,
                        NewsroomDedupeDecision.scope == "published",
                        NewsroomDedupeDecision.outcome == "excluded",
                    )
                )
            ).all()
        )
        ratings = (
            (
                await database.execute(
                    select(NewsroomEditorRating).where(NewsroomEditorRating.edition_date == day)
                )
            )
            .scalars()
            .all()
        )
        editions = {
            edition.market_code: edition
            for edition in (
                await database.scalars(
                    select(NewsroomEdition).where(NewsroomEdition.edition_date == day)
                )
            ).all()
        }
        items = (
            await database.execute(
                select(NewsroomEditionItem, NewsroomEvent.working_title)
                .join(NewsroomEvent, NewsroomEvent.id == NewsroomEditionItem.event_id)
                .join(NewsroomEdition, NewsroomEdition.id == NewsroomEditionItem.edition_id)
                .where(
                    NewsroomEdition.edition_date == day, NewsroomEditionItem.removed_at.is_(None)
                )
                .order_by(NewsroomEditionItem.rank)
            )
        ).all()
    info = await _event_info(context, day)
    total, relevant, per_market = await _relevance(context, day)
    days: list[MarketDay] = []
    for market in MARKET_CODES:
        edition = editions.get(market)
        reference_picks, reference_ratings = _reference_picks(context, day, market)
        reference_stars = {row["event_id"]: int(row["stars"]) for row in reference_ratings}
        mine = sorted((r for r in ratings if r.market_code == market), key=lambda r: r.position)
        ranking = pipeline.rank_candidates(
            titles, articles, market, excluded=excluded, variant=context.variant, limit=False
        )
        by_id = {candidate.event_id: candidate for candidate in ranking}
        picked = [
            (item, title)
            for item, title in items
            if edition is not None and item.edition_id == edition.id
        ]
        ranks = {item.event_id: rank for rank, (item, _) in enumerate(picked, start=1)}
        if mine:
            rows = {
                rating.event_id: _row(
                    rating.event_id,
                    rating.event_title,
                    info,
                    position=rating.position,
                    score=rating.score,
                    stars=rating.stars,
                    picked=rating.event_id in ranks,
                    rank=ranks.get(rating.event_id),
                    follow_up=rating.follow_up,
                    headlines=[t for t, _ in by_id[rating.event_id].headlines]
                    if rating.event_id in by_id
                    else (),
                )
                for rating in mine
            }
        else:
            rows = {
                candidate.event_id: _row(
                    candidate.event_id,
                    candidate.working_title,
                    info,
                    position=position,
                    score=candidate.score,
                    stars=None,
                    picked=candidate.event_id in ranks,
                    rank=ranks.get(candidate.event_id),
                    headlines=[t for t, _ in candidate.headlines],
                )
                for position, candidate in enumerate(ranking[: assembly.CANDIDATE_LIMIT], start=1)
            }
        picks = [
            rows.get(item.event_id)
            or _row(
                item.event_id,
                title,
                info,
                position=None,
                score=item.editor_score,
                stars=item.stars,
                picked=True,
                rank=rank,
            )
            for rank, (item, title) in enumerate(picked, start=1)
        ]
        quota = [
            assembly.Rated(item.event_id, item.stars or 0, item.editor_score or 0)
            for item, _ in picked
        ]
        days.append(
            MarketDay(
                edition_date=day.isoformat(),
                market=market,
                selection_mode=edition.selection_mode if edition is not None else "none",
                candidates=list(rows.values()),
                picks=picks,
                tracked=_tracked(context, market, ranking, rows, info),
                reference_picks=reference_picks,
                reference_stars=reference_stars,
                articles=total,
                relevant=relevant,
                market_relevant=per_market[market],
                five_star_review=pipeline.five_star_review(quota, variant=context.variant),
            )
        )
    await _publish_day(context, day)
    return days


async def _publish_day(context: _Context, day: date) -> None:
    """Mark the day's drafts published so the next day's repeat screen sees them.

    The repeat screen shows the published headline, which analysis writes and
    a replay never runs: a replayed pick borrows the headline of the production
    event it stands for, or its working title when production had none.
    """
    published_at = clock.auto_publish_at(day)
    async with context.sessions() as database:
        await database.execute(
            update(NewsroomEdition)
            .where(NewsroomEdition.edition_date == day)
            .values(status="published", published_at=published_at)
        )
        borrowed = await database.execute(
            text(
                "WITH picked AS ("
                "  SELECT DISTINCT i.event_id FROM newsroom_edition_items i"
                "  JOIN newsroom_editions e ON e.id = i.edition_id"
                "  WHERE e.edition_date = :day AND i.removed_at IS NULL"
                "), origin AS ("
                "  SELECT DISTINCT ON (a.event_id) a.event_id, o.event_id AS prod_event_id"
                "  FROM newsroom_articles a JOIN replay_article_origin o ON o.article_id = a.id"
                "  WHERE a.event_id IN (SELECT event_id FROM picked)"
                "  GROUP BY a.event_id, o.event_id ORDER BY a.event_id, count(*) DESC, o.event_id"
                ") "
                "UPDATE newsroom_events ev SET"
                "  headline_zh_hant = coalesce(p.headline_zh_hant, ev.working_title),"
                "  summary_zh_hant = coalesce(p.summary_zh_hant, '')"
                " FROM picked LEFT JOIN origin ON origin.event_id = picked.event_id"
                " LEFT JOIN replay_prod_events p"
                "  ON p.event_id = coalesce(origin.prod_event_id, picked.event_id)"
                " WHERE ev.id = picked.event_id AND ev.headline_zh_hant IS NULL"
                " RETURNING p.headline_zh_hant IS NULL"
            ),
            {"day": day},
        )
        flags = [row[0] for row in borrowed]
        await database.commit()
    if flags:
        context.note(
            day,
            f"供下一天前版篩檢: {len(flags)} 則入選事件沒有標題, 借用對應正式事件的標題"
            f" (其中 {sum(flags)} 則正式也沒有, 改用工作標題)",
        )


async def _reset_window(context: _Context, day: date) -> list[uuid.UUID]:
    """Drop the window's events and put its triaged articles back before triage."""
    async with context.sessions() as database:
        await database.execute(text("SET LOCAL session_replication_role = replica"))
        article_ids = list(
            (
                await database.scalars(
                    select(NewsroomArticle.id)
                    .where(
                        NewsroomArticle.edition_date == day, NewsroomArticle.triage_status != "idle"
                    )
                    .order_by(NewsroomArticle.first_seen_at, NewsroomArticle.id)
                )
            ).all()
        )
        await database.execute(
            update(NewsroomArticle)
            .where(NewsroomArticle.id.in_(article_ids))
            .values(
                event_id=None,
                relevant=None,
                topic=None,
                story_type=None,
                triage_signals=None,
                market_scores={},
                triaged_at=None,
                triage_status="pending",
                triage_attempts=0,
                triage_next_attempt_at=None,
                triage_error_code=None,
            )
        )
        await database.execute(
            text("DELETE FROM newsroom_events WHERE edition_date = :day"), {"day": day}
        )
        await database.commit()
    return article_ids


async def _triage_window(context: _Context, day: date) -> None:
    article_ids = await _reset_window(context, day)
    window = context.options.concurrency
    gate = TriageGate(len(article_ids), window)
    context.gate_holder[0] = gate
    handler = triage._triage_handler(context.runtime)
    outcomes: Counter[str] = Counter()
    errors: Counter[str] = Counter()

    async def one(index: int, article_id: uuid.UUID) -> None:
        _member.set(index)
        _article.set(article_id)
        await gate.wait_start(index)
        outcome = "pending"
        try:
            for attempt in range(1, queue.TRIAGE.max_attempts + 1):
                async with context.sessions() as database:
                    await database.execute(
                        update(NewsroomArticle)
                        .where(NewsroomArticle.id == article_id)
                        .values(triage_status="pending", triage_attempts=attempt)
                    )
                    await database.commit()
                try:
                    outcome = await queue.run_claimed(
                        context.sessions, Claim(queue.TRIAGE, article_id, attempt), handler
                    )
                except ReplayCacheMiss:
                    raise
                except Exception as error:
                    errors[type(error).__name__] += 1
                    outcome = "pending"
                if outcome == "fatal":
                    raise ReplayError("a triage call failed fatally (provider key or quota?)")
                if outcome != "pending":
                    break
            outcomes[outcome] += 1
        finally:
            await gate.finish(index)

    orm_event.listen(NewsroomEvent, "init", _replay_event_id)
    try:
        async with asyncio.TaskGroup() as group:
            for index, article_id in enumerate(article_ids):
                group.create_task(one(index, article_id))
    finally:
        orm_event.remove(NewsroomEvent, "init", _replay_event_id)
        context.gate_holder[0] = None
    async with context.sessions() as database:
        failures = Counter(
            (
                await database.scalars(
                    select(NewsroomArticle.triage_error_code).where(
                        NewsroomArticle.id.in_(article_ids),
                        NewsroomArticle.triage_status == "failed",
                    )
                )
            ).all()
        )
        events = await database.scalar(
            text("SELECT count(*) FROM newsroom_events WHERE edition_date = :day"), {"day": day}
        )
    missing = failures.pop("triage_embedding_missing", 0)
    context.note(
        day,
        f"重跑初篩: {len(article_ids)} 篇 (成功 {outcomes['done']}、失敗 {sum(failures.values())}"
        f"、缺向量略過 {missing}), 開出 {events} 個事件"
        + (f"; 失敗原因 {dict(failures)}" if failures else "")
        + (f"; 非預期錯誤 {dict(errors)}" if errors else ""),
    )


# --- Entry point -----------------------------------------------------------------------


def _git_revision() -> str:
    def git(*args: str) -> str:
        result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
        return result.stdout.strip()

    revision = git("rev-parse", "--short", "HEAD") or "unknown"
    return revision + ("-dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def _notes(options: RunOptions) -> list[str]:
    notes = [
        "正式資料庫只以唯讀交易匯出; 所有寫入只發生在暫存 pgvector 資料庫的本次複本, 跑完即刪除。",
        "Slack webhook 一律為空, 通知只記錄在記憶體中的 LogNotifier; 不執行分析、上架或通知。",
        "模型呼叫先查快取 (鍵: 模型、system prompt 全文、user payload 與呼叫對象的 SHA-256, "
        "事件 id 以出現順序代號化); 文章向量沿用正式庫, 只有工作標題 (重複與前版篩檢用)"
        "的向量會呼叫 embedding API 並快取。",
        "relevant 比例 = 當日收稿窗內 relevant 文章數 ÷ 文章總數; 市場達標文章 = 該市場分數 ≥ "
        f"{scoring.RELEVANT_SCORE} 的文章數。",
        "非台灣50個股稿 = 入選事件中主題為 companies、且標題與代表報導都比對不到台灣 50 成分股"
        " (名稱、別名或代號)的則數 ÷ 版面則數; 比對只看字面, 無法判斷報導主角。",
        "impact = 事件內 relevant 報導的最高 triage impact。",
    ]
    if options.stages == "score":
        notes += [
            "score: 不呼叫模型; 以已存的初篩判斷依 variant 重算分數, "
            "星等沿用正式 newsroom_editor_ratings, 前版排除、前版後續與故事群組沿用正式 "
            "newsroom_dedupe_decisions。新進前 40 但正式沒有星等的事件標為「未評」且不入選。",
        ]
    else:
        notes += [
            f"{options.stages}: 以真實組稿程式重跑重複篩檢、前版篩檢、精選與配額。",
            "後一天的前版篩檢對照本次重播產生的前一天版面; 範圍開始前的日期才用正式資料。"
            "入選事件的標題借用對應的正式事件 (分析階段不重跑), 正式也沒有時用工作標題。",
        ]
    if options.stages == "editor":
        notes.append(
            "editor: 從正式組稿之後的事件狀態出發, 正式組稿時已自動合併的重複事件不會再被提出。"
        )
    if options.stages == "triage":
        notes.append(
            "triage: 清空每日事件後依 first_seen_at 順序以真實 triage handler 重新初篩與歸屬, "
            f"並行 {options.concurrency}; 每篇只看得到排在它前 {options.concurrency} 篇以前的事件"
            " (固定順序, 重跑結果相同)。正式環境是隨收稿時間陸續初篩, 事件分群可能因此不同。"
            "新開事件的 id 由開出它的文章 id 推導 (固定), "
            "與正式比較時以文章多數所屬的正式事件對應。",
        )
    return notes


def _run_name(environment: ReplayEnvironment, options: RunOptions, now: datetime) -> Path:
    stamp = now.astimezone(clock.TAIPEI).strftime("%Y%m%d-%H%M")
    base = environment.runs_dir / f"{stamp}-{options.variant.name}-{options.stages}"
    path, suffix = base, 2
    while path.exists():
        path, suffix = Path(f"{base}-{suffix}"), suffix + 1
    return path


async def run(environment: ReplayEnvironment, options: RunOptions) -> Path:
    """Replay ``options.snapshot`` and write its run directory; returns the directory."""
    pipeline.require_supported(options.variant)
    if not 1 <= options.concurrency <= MAX_CONCURRENCY:
        raise ReplayError(f"--concurrency must be between 1 and {MAX_CONCURRENCY}")
    # Nothing a replay starts may reach Slack, whatever the shell exported.
    os.environ.pop(SLACK_ENVIRONMENT, None)
    metadata = snapshot.prepare(environment, options.snapshot)
    reference = snapshot.load_reference(environment, options.snapshot)
    started = datetime.now(UTC)
    run_dir = _run_name(environment, options, started)
    database = f"run_{started:%Y%m%d%H%M%S}_{options.variant.name}_{options.stages}"
    snapshot.create_database(environment, database, template=options.snapshot.template)
    try:
        snapshot.migrate(environment, database)
        settings = _settings(environment, options, database)
        engine = create_engine(settings)
        stats = CacheStats()
        gate_holder: list[TriageGate | None] = [None]
        runtime = _runtime(environment, options, settings, engine, gate_holder, stats)
        context = _Context(environment, options, runtime, engine, reference, gate_holder)
        days: list[MarketDay] = []
        try:
            for day in options.snapshot.dates():
                logger.info("newsroom.replay_day", extra={"edition_date": day.isoformat()})
                if options.stages == "score":
                    days += await _score_day(context, day)
                    continue
                if options.stages == "triage":
                    await _triage_window(context, day)
                else:
                    await _rescore(context, day)
                days += await _assemble_day(context, day)
        finally:
            await runtime.aclose()
            await engine.dispose()
    finally:
        if not options.keep_database:
            snapshot.drop_database(environment, database)
    meta = {
        "start": options.snapshot.start.isoformat(),
        "end": options.snapshot.end.isoformat(),
        "variant": options.variant.name,
        "stages": options.stages,
        "concurrency": options.concurrency,
        "started_at": started.astimezone(clock.TAIPEI).isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).astimezone(clock.TAIPEI).isoformat(timespec="seconds"),
        "git_revision": _git_revision(),
        "snapshot": options.snapshot.name,
        "source_revision": metadata.get("source_revision"),
        "exported_at": metadata.get("exported_at"),
        "triage_model": settings.newsroom_triage_model,
        "editor_model": settings.newsroom_editor_model,
        "embedding_model": settings.newsroom_embedding_model,
        "database": database if options.keep_database else None,
        "run_dir": str(run_dir),
        "cache": stats.as_dict(),
        "notes": _notes(options),
        "day_notes": context.day_notes,
        "tracks": [
            {"label": track.label, "markets": list(track.markets), "pattern": track.pattern}
            for track in options.tracks
        ],
    }
    write_run(run_dir, meta, days)
    return run_dir


def write_run(run_dir: Path, meta: dict[str, Any], days: Sequence[MarketDay]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    for day in days:
        path = run_dir / day.edition_date / f"{day.market}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(day.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "run.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "summary.md").write_text(summary(meta, days), encoding="utf-8")


def read_run(run_dir: Path) -> tuple[dict[str, Any], list[MarketDay]]:
    meta: dict[str, Any] = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    days = [
        MarketDay.load(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(run_dir.glob("*/*.json"))
    ]
    order = {market: index for index, market in enumerate(MARKET_CODES)}
    days.sort(key=lambda day: (day.edition_date, order.get(day.market, 99)))
    return meta, days
