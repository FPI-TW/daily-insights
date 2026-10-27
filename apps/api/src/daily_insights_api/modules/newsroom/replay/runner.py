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
``retriage`` rebuilds a base variant's triage replay from the cache, re-judges
only the articles a prompt change can affect under the run's variant (see
``retriage``), keeps every event assignment, and assembles under the variant.

``--rep`` draws the assembly's model calls (and a re-triage's) again as an
independent repetition; ``--triage-rep`` does the same for a full triage pass.
Repetition 1 is what earlier runs cached. A market day whose editor fell back
to scores, whose triage pass hit provider failures, or that was skipped is
marked invalid and left out of every comparison. Rerunning a range after a
failure only asks again what failed: every successful answer is cached.

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
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import event as orm_event
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.newsroom import assembly, clock, queue, scoring, triage
from daily_insights_api.modules.newsroom.contracts import TriageResult
from daily_insights_api.modules.newsroom.ingestion.allowlist import MANUAL_KIND
from daily_insights_api.modules.newsroom.models import (
    MARKET_CODES,
    NewsroomArticle,
    NewsroomDedupeDecision,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEditorRating,
    NewsroomEvent,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.notifier import LogNotifier
from daily_insights_api.modules.newsroom.providers import (
    CallAudit,
    Embedder,
    JsonModel,
    OpenAICompatibleEmbedder,
    OpenAICompatibleJsonModel,
)
from daily_insights_api.modules.newsroom.queue import Claim, FatalStageError, RetryableStageError
from daily_insights_api.modules.newsroom.replay import pipeline, retriage, snapshot
from daily_insights_api.modules.newsroom.replay.cache import (
    ApiFailures,
    CachedEmbedder,
    CachedJsonModel,
    CacheStats,
    ReplayCacheMiss,
    ResponseCache,
    replay_rep,
    request_key,
    watch_usage,
)
from daily_insights_api.modules.newsroom.replay.costs import Ledger, ledger_total, read_ledger
from daily_insights_api.modules.newsroom.replay.metrics import (
    DEFAULT_TRACKS,
    EventRow,
    MarketDay,
    Track,
    TrackedRow,
    invalid_reason_for,
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

Stages = Literal["score", "editor", "triage", "retriage"]
STAGES: tuple[Stages, ...] = ("score", "editor", "triage", "retriage")
MAX_CONCURRENCY = 8
HEADLINES_SHOWN = 3
SLACK_ENVIRONMENT = "DAILY_INSIGHTS_NEWSROOM_SLACK_WEBHOOK_URL"
REVIEW_BASE_URL = "http://replay.invalid"
# Fatal triage codes that would fail every article: stop instead of going on.
ABORTING_FATAL_CODES = ("_http_401", "_http_402", "_http_403", "_configuration_missing")
# Triage failures that mean the provider was unavailable rather than the article bad
# (a 400 is one article's content refused; invalid JSON is the model's answer).
PROVIDER_FAILURE_PREFIX = "triage_provider_"
ARTICLE_FAILURE_CODES = ("triage_provider_http_400", "triage_provider_invalid_json")
RETRIAGE_ATTEMPTS = 3
# The p0b round's hold-out period (_common.md): never looked at, only fed in.
HOLDOUT = (date(2026, 10, 14), date(2026, 10, 17))
DEFAULT_ROUND = "p0b"


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
    # Repetition of the assembly's calls and of a re-triage's calls.
    rep: int = 1
    # Repetition of a full triage pass (also the base pass of a re-triage).
    triage_rep: int = 1
    # ``retriage`` only: the variant whose triage replay supplies the events.
    base_variant: SelectionVariant | None = None
    selection: retriage.Selection = field(default_factory=retriage.Selection)
    # ``retriage`` only: a variant whose energy re-judgement runs first (p0b),
    # its repetition, and what the run variant itself re-judges.
    via_variant: SelectionVariant | None = None
    via_rep: int = 1
    select_mode: retriage.SelectMode = "energy"
    # Published editions in this range are pipeline input only: decisions that
    # match one keep its date and drop its title.
    holdout: tuple[date, date] | None = HOLDOUT
    # ``retriage`` only: rebuild the base and estimate, call nothing live.
    dry_run: bool = False
    # Days not replayed, each marked invalid with its reason.
    skip_days: tuple[tuple[date, str], ...] = ()
    # The ledger every live call of this round is written to.
    round: str = DEFAULT_ROUND

    @property
    def skipped(self) -> dict[date, str]:
        return dict(self.skip_days)


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
    # ``retriage``: the base variant's runtime, recording its triage requests.
    base_runtime: Runtime | None = None
    recorder: retriage.RecordingModel | None = None
    stats: CacheStats | None = None
    retriaged: list[dict[str, Any]] = field(default_factory=list)
    # Events the event-date rule held back, and every screen decision of the run.
    blocked: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    estimate: retriage.Estimate = field(default_factory=retriage.Estimate)
    # Days whose triage pass the provider failed, with the count.
    triage_failures: dict[date, int] = field(default_factory=dict)

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        return self.runtime.session_factory

    @property
    def variant(self) -> SelectionVariant:
        return self.options.variant

    @property
    def base_variant(self) -> SelectionVariant:
        return self.options.base_variant or self.options.variant

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
    ledger: Ledger,
    failures: ApiFailures,
) -> Runtime:
    cache = ResponseCache(environment.cache_dir)

    def live_model() -> JsonModel:
        model = OpenAICompatibleJsonModel(
            base_url=settings.newsroom_llm_base_url,
            api_key=_key(environment, "NEWSROOM_LLM_API_KEY"),
            timeout_seconds=settings.newsroom_llm_timeout_seconds,
            user_agent=settings.newsroom_llm_user_agent,
            session_header=settings.newsroom_llm_session_header,
        )
        watch_usage(model)
        return model

    def live_embedder() -> Embedder:
        embedder = OpenAICompatibleEmbedder(
            base_url=settings.newsroom_embedding_base_url,
            api_key=_key(environment, "NEWSROOM_EMBEDDING_API_KEY"),
            model=settings.newsroom_embedding_model,
            timeout_seconds=settings.newsroom_embedding_timeout_seconds,
        )
        watch_usage(embedder)
        return embedder

    return Runtime(
        settings=settings,
        session_factory=create_session_factory(engine),
        llm=CachedJsonModel(
            cache,
            stats,
            live=None if options.offline or options.dry_run else live_model,
            gate=_HeldGate(gate_holder),
            ledger=ledger,
            failures=failures,
        ),
        embedder=CachedEmbedder(
            cache,
            stats,
            model=settings.newsroom_embedding_model,
            live=None if options.offline or options.dry_run else live_embedder,
            ledger=ledger,
            failures=failures,
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
    """Recompute stored triage's market scores and relevance under the variant.

    Under the event-date rule an old-dated article scores without new facts,
    exactly as the triage handler scores it.
    """
    changed = incomplete = newly_relevant_unassigned = dropped = dated_old = 0
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
                    NewsroomSource.kind,
                )
                .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
                .where(
                    NewsroomArticle.edition_date == day,
                    NewsroomArticle.triage_status == "done",
                )
            )
        ).all()
        for article_id, topic, story_type, signals, stored, was_relevant, event_id, kind in rows:
            result = pipeline.triage_result(topic=topic, story_type=story_type, signals=signals)
            if result is None:
                incomplete += 1
                continue
            scored, old = pipeline.scored_result(
                result, variant=context.variant, edition_date=day, manual=kind == MANUAL_KIND
            )
            dated_old += old
            scores = pipeline.market_scores(scored, variant=context.variant)
            relevant = pipeline.is_relevant(scores)
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
        f"需 triage 重跑才會進入事件); {incomplete} 篇缺少可重算的初篩判斷, 沿用已存分數"
        + (f"; event_date 過舊而視為無新事實 {dated_old} 篇" if dated_old else ""),
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
    reason: str = "",
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
        reason=reason,
        impact=details.impact if details else None,
        escalation=details.escalation if details else None,
        topic=details.topic if details else None,
        headlines=shown[:HEADLINES_SHOWN],
        article_titles=list(details.titles if details else ()),
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
                    reason=rating.reason or "",
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
    await _record_decisions(context, day)
    await _publish_day(context, day)
    failures = context.triage_failures.get(day)
    for market_day in days:
        market_day.invalid_reason = invalid_reason_for(market_day.selection_mode) or (
            f"初篩有 {failures} 篇因供應商連線失敗而沒有結果" if failures else None
        )
    return days


async def _record_decisions(context: _Context, day: date) -> None:
    """Keep the day's duplicate and repeat screen decisions for the run's reports.

    A repeat match names the edition it was published in; one inside the
    hold-out period keeps that date only, never its title.
    """
    async with context.sessions() as database:
        rows = (
            await database.execute(
                select(NewsroomDedupeDecision).where(NewsroomDedupeDecision.edition_date == day)
            )
        ).scalars()
        decisions = list(rows)
        published = {
            row[0]: row[1]
            for row in await database.execute(
                select(NewsroomEditionItem.event_id, func.max(NewsroomEdition.edition_date))
                .join(NewsroomEdition, NewsroomEdition.id == NewsroomEditionItem.edition_id)
                .where(
                    NewsroomEdition.status == "published",
                    NewsroomEdition.edition_date < day,
                    NewsroomEditionItem.event_id.in_(
                        [decision.matched_event_id for decision in decisions]
                    ),
                )
                .group_by(NewsroomEditionItem.event_id)
            )
        }
    holdout = context.options.holdout
    for decision in decisions:
        matched_on = published.get(decision.matched_event_id)
        hidden = (
            decision.scope == "published"
            and holdout is not None
            and matched_on is not None
            and holdout[0] <= matched_on <= holdout[1]
        )
        context.decisions.append(
            {
                "edition_date": day.isoformat(),
                "scope": decision.scope,
                "outcome": decision.outcome,
                "event_id": str(decision.event_id),
                "event_title": decision.event_title,
                "matched_event_id": str(decision.matched_event_id),
                "matched_title": None if hidden else decision.matched_title,
                "matched_edition_date": matched_on.isoformat() if matched_on else None,
                "holdout_match": hidden,
                "title_similarity": decision.title_similarity,
                "article_similarity": decision.article_similarity,
                "threshold": decision.threshold,
                "shared_figure": decision.shared_figure,
                "new_facts": decision.new_facts,
            }
        )


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


async def _triage_window(context: _Context, day: date, runtime: Runtime | None = None) -> None:
    article_ids = await _reset_window(context, day)
    window = context.options.concurrency
    gate = TriageGate(len(article_ids), window)
    context.gate_holder[0] = gate
    handler = triage._triage_handler(runtime or context.runtime)
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
                    async with context.sessions() as database:
                        code = await database.scalar(
                            select(NewsroomArticle.triage_error_code).where(
                                NewsroomArticle.id == article_id
                            )
                        )
                    # A key, balance or configuration failure would fail every
                    # article; anything else (a provider refusing one article's
                    # content) fails that article only, as in production.
                    if code is None or code.endswith(ABORTING_FATAL_CODES):
                        raise ReplayError(f"a triage call failed fatally ({code})")
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
    except* (ReplayError, ReplayCacheMiss) as group_error:
        # Report the first reason rather than the task group around it.
        raise group_error.exceptions[0] from None
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
    unreachable = sum(
        count
        for code, count in failures.items()
        if code and code.startswith(PROVIDER_FAILURE_PREFIX) and code not in ARTICLE_FAILURE_CODES
    )
    if unreachable:
        context.triage_failures[day] = unreachable
    context.note(
        day,
        f"重跑初篩: {len(article_ids)} 篇 (成功 {outcomes['done']}、失敗 {sum(failures.values())}"
        f"、缺向量略過 {missing}), 開出 {events} 個事件"
        + (f"; 失敗原因 {dict(failures)}" if failures else "")
        + (f"; 非預期錯誤 {dict(errors)}" if errors else ""),
    )


# --- retriage --------------------------------------------------------------------------


async def _judged(context: _Context, day: date) -> list[retriage.Judged]:
    async with context.sessions() as database:
        rows = (
            await database.execute(
                select(
                    NewsroomArticle.id,
                    NewsroomArticle.title,
                    NewsroomArticle.topic,
                    NewsroomArticle.story_type,
                    NewsroomArticle.triage_signals,
                    NewsroomArticle.relevant,
                    NewsroomArticle.event_id,
                    NewsroomArticle.published_at,
                    NewsroomSource.kind,
                )
                .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
                .where(NewsroomArticle.edition_date == day, NewsroomArticle.triage_status == "done")
                .order_by(NewsroomArticle.first_seen_at, NewsroomArticle.id)
            )
        ).all()
    return [
        retriage.Judged(
            article_id=row[0],
            edition_date=day,
            title=row[1],
            topic=row[2],
            story_type=row[3],
            signals=dict(row[4] or {}),
            relevant=bool(row[5]),
            event_id=row[6],
            published_at=row[7],
            manual=row[8] == MANUAL_KIND,
        )
        for row in rows
    ]


def _judgement(result: TriageResult | None) -> dict[str, Any]:
    if result is None:
        return {}
    return {
        "impact": result.impact,
        "escalation": result.escalation,
        "story_type": result.story_type,
        "new_facts": result.new_facts,
        "event_date": result.event_date.isoformat() if result.event_date else None,
    }


def retriage_payload(
    request: retriage.Request, article: retriage.Judged, variant: SelectionVariant
) -> dict[str, Any]:
    """The base call's payload as ``variant`` asks it (the event-date prompt adds published_at)."""
    if not variant.event_date_rule:
        return request.payload
    original = request.payload["article"]
    rebuilt = {key: original[key] for key in ("title", "source", "summary") if key in original}
    rebuilt["published_at"] = (
        None
        if article.published_at is None
        else article.published_at.astimezone(clock.TAIPEI).isoformat()
    )
    if "body_excerpt" in original:
        rebuilt["body_excerpt"] = original["body_excerpt"]
    return {**request.payload, "article": rebuilt}


async def _ask_again(
    context: _Context,
    article: retriage.Judged,
    request: retriage.Request,
    variant: SelectionVariant,
) -> TriageResult | str:
    """``variant``'s triage answer to the base call's request, or the failure code."""
    prompt = triage.load_triage_prompt(variant=variant)
    code = "retriage_failed"
    for _ in range(RETRIAGE_ATTEMPTS):
        try:
            async with context.sessions() as database:
                result = await context.runtime.llm.complete(
                    database,
                    model=request.model,
                    system=prompt.text,
                    payload=retriage_payload(request, article, variant),
                    result_type=TriageResult,
                    audit=CallAudit("triage", article.article_id, prompt.version),
                )
                await database.commit()
            return result
        except RetryableStageError as error:
            code = error.code
        except FatalStageError as error:
            if error.code.endswith(ABORTING_FATAL_CODES):
                raise ReplayError(f"a re-triage call failed fatally ({error.code})") from error
            return error.code
    return code


def _uncached(
    context: _Context,
    chosen: Sequence[tuple[str, retriage.Judged]],
    variant: SelectionVariant,
    rep: int,
) -> list[retriage.Request]:
    """The requests of ``chosen`` the cache cannot answer (what a live run would pay for)."""
    assert context.recorder is not None
    cache = ResponseCache(context.environment.cache_dir)
    prompt = triage.load_triage_prompt(variant=variant)
    missing: list[retriage.Request] = []
    for _, article in chosen:
        request = context.recorder.requests[article.article_id]
        payload = retriage_payload(request, article, variant)
        key, _ = request_key(
            model=request.model,
            system=prompt.text,
            payload=payload,
            subject=article.article_id,
            rep=rep,
        )
        if cache.answer(key, 0) is None:
            missing.append(retriage.Request(prompt.text, payload, request.model))
    return missing


async def _rejudge(
    context: _Context,
    day: date,
    chosen: Sequence[tuple[str, retriage.Judged]],
    *,
    variant: SelectionVariant,
    rep: int,
) -> None:
    """Ask ``variant`` about each chosen article; apply the answers of the targets."""
    assert context.recorder is not None
    requests = context.recorder.requests
    semaphore = asyncio.Semaphore(context.options.concurrency)

    async def one(group: str, article: retriage.Judged) -> None:
        async with semaphore:
            answer = await _ask_again(context, article, requests[article.article_id], variant)
        base = pipeline.triage_result(
            topic=article.topic, story_type=article.story_type, signals=article.signals
        )
        record: dict[str, Any] = {
            "edition_date": day.isoformat(),
            "step": variant.name,
            "group": group,
            "article_id": str(article.article_id),
            "event_id": str(article.event_id) if article.event_id else None,
            "title": article.title,
            "topic": article.topic,
            "published_at": article.published_at.isoformat() if article.published_at else None,
            "base": _judgement(base) | {"relevant": article.relevant},
            "applied": False,
        }
        if isinstance(answer, str):
            record["error"] = answer
        elif base is not None:
            merged = retriage.merged_result(base, answer)
            _, dated_old = pipeline.scored_result(
                merged, variant=variant, edition_date=day, manual=article.manual
            )
            record["new"] = _judgement(merged) | {"dated_old": dated_old}
            if group == "target":
                record["applied"] = True
                signals = scoring.triage_signals(merged) | {
                    "stale": article.signals.get("stale", False),
                    "retriaged": variant.name,
                }
                if variant.event_date_rule:
                    signals["dated_old"] = dated_old
                async with context.sessions() as database:
                    await database.execute(
                        update(NewsroomArticle)
                        .where(NewsroomArticle.id == article.article_id)
                        .values(story_type=merged.story_type, triage_signals=signals)
                    )
                    await database.commit()
        context.retriaged.append(record)

    with replay_rep(rep):
        async with asyncio.TaskGroup() as group_:
            for group, article in chosen:
                group_.create_task(one(group, article))
    failed = sum(
        1
        for row in context.retriaged
        if row["edition_date"] == day.isoformat() and row["step"] == variant.name and "error" in row
    )
    if failed:
        context.note(day, f"{variant.name} 重判失敗 {failed} 篇, 沿用之前的判斷")


async def _pool(context: _Context, day: date) -> set[uuid.UUID]:
    """Events in some market's top 40 under the run's variant, as the stored scores rank them."""
    async with context.sessions() as database:
        titles, articles = await assembly._load_event_articles(database, day)
    return {
        candidate.event_id
        for market in MARKET_CODES
        for candidate in pipeline.rank_candidates(
            titles, articles, market, excluded=(), variant=context.variant
        )
    }


async def _block_old_events(context: _Context, day: date) -> None:
    """Approximate "an old-dated article never opens an event" after the fact.

    Re-judging cannot reopen events, so an event whose relevant articles are all
    dated before the window counts as one that would not have been opened: its
    articles leave it and it drops out of assembly.
    """
    articles = [row for row in await _judged(context, day) if row.relevant and row.event_id]
    members: dict[uuid.UUID, list[retriage.Judged]] = defaultdict(list)
    for article in articles:
        assert article.event_id is not None
        members[article.event_id].append(article)
    blocked: list[uuid.UUID] = []
    for event_id, rows in members.items():
        flags = []
        for row in rows:
            result = pipeline.triage_result(
                topic=row.topic, story_type=row.story_type, signals=row.signals
            )
            flags.append(
                result is not None
                and pipeline.scored_result(
                    result, variant=context.variant, edition_date=day, manual=row.manual
                )[1]
            )
        if flags and all(flags):
            blocked.append(event_id)
    if not blocked:
        context.note(day, "event_date 規則: 沒有事件因所有文章都過舊而被擋下")
        return
    async with context.sessions() as database:
        titles: dict[uuid.UUID, str] = {
            row[0]: row[1]
            for row in await database.execute(
                select(NewsroomEvent.id, NewsroomEvent.working_title).where(
                    NewsroomEvent.id.in_(blocked)
                )
            )
        }
        await database.execute(
            update(NewsroomArticle)
            .where(NewsroomArticle.event_id.in_(blocked))
            .values(event_id=None, relevant=False)
        )
        await database.commit()
    for event_id in blocked:
        context.blocked.append(
            {
                "edition_date": day.isoformat(),
                "event_id": str(event_id),
                "working_title": titles.get(event_id, ""),
                "articles": [
                    {
                        "title": row.title,
                        "event_date": row.signals.get("event_date"),
                        "published_at": row.published_at.isoformat() if row.published_at else None,
                    }
                    for row in members[event_id]
                ],
            }
        )
    context.note(day, f"event_date 規則: {len(blocked)} 個事件的文章全部過舊, 視為被擋下")


async def _retriage_day(context: _Context, day: date) -> bool:
    """Rebuild the base triage and re-judge the chosen articles; False on a dry run."""
    assert context.base_runtime is not None and context.recorder is not None
    assert context.stats is not None
    options = context.options
    context.recorder.requests.clear()
    triage_label = f"triage {triage.load_triage_prompt(variant=context.base_variant).version}"
    before = context.stats.count_label(triage_label)
    with replay_rep(options.triage_rep):
        await _triage_window(context, day, runtime=context.base_runtime)
    after = context.stats.count_label(triage_label)
    requests = context.recorder.requests
    base_chars = sum(
        len(request.system)
        + len(json.dumps(request.payload, ensure_ascii=False, separators=(",", ":")))
        for request in requests.values()
    )
    tokens_per_char = (after.input_tokens - before.input_tokens) / max(base_chars, 1)
    output_per_call = (after.output_tokens - before.output_tokens) / max(
        after.calls - before.calls, 1
    )

    def estimate(missing: Sequence[retriage.Request], label: str) -> None:
        context.estimate.add(
            day,
            missing,
            system=missing[0].system if missing else "",
            tokens_per_char=tokens_per_char,
            output_per_call=output_per_call,
            label=label,
        )

    via = options.via_variant
    if via is not None:
        targets, _ = retriage.select(await _judged(context, day), options.selection)
        chosen = [("target", article) for article in targets if article.article_id in requests]
        missing = _uncached(context, chosen, via, options.via_rep)
        estimate(missing, f"{via.name} rep{options.via_rep}")
        context.note(
            day,
            f"目標重判 ({via.name}, rep {options.via_rep}): 能源/航運目標 {len(chosen)} 篇"
            f" (快取未命中 {len(missing)} 篇)",
        )
        if options.dry_run and missing:
            context.note(day, f"dry run: {via.name} 有未快取的重判, 後續選取以 base 狀態近似")
        else:
            await _rejudge(context, day, chosen, variant=via, rep=options.via_rep)
            await _rescore(context, day)
    articles = await _judged(context, day)
    if options.select_mode == "energy":
        targets, controls = retriage.select(articles, options.selection)
    elif options.select_mode == "pool":
        if via is None and not options.dry_run:
            await _rescore(context, day)
        targets, controls = retriage.select_pool(articles, await _pool(context, day)), []
    else:
        targets, controls = [], []
    chosen = [
        (group, article)
        for group, members in (("target", targets), ("control", controls))
        for article in members
        if article.article_id in requests
    ]
    missing = _uncached(context, chosen, context.variant, options.rep) if chosen else []
    estimate(missing, f"{context.variant.name} rep{options.rep}")
    context.note(
        day,
        f"目標重判 ({context.variant.name}, rep {options.rep}, 選取 {options.select_mode}):"
        f" 以 {context.base_variant.name} 的初篩重建事件分群, 目標 {len(targets)} 篇、"
        f"對照 {len(controls)} 篇 (快取未命中 {len(missing)} 篇)",
    )
    if options.dry_run:
        return False
    if chosen:
        await _rejudge(context, day, chosen, variant=context.variant, rep=options.rep)
    if context.variant.event_date_rule:
        await _block_old_events(context, day)
    # Scores and relevance follow the stored judgements under the run's variant.
    await _rescore(context, day)
    return True


def _void_failed_calls(
    context: _Context,
    day: date,
    days: Sequence[MarketDay],
    failed: Sequence[tuple[str, str]],
) -> None:
    """A day with a call that outlasted every retry is not a result: mark it invalid."""
    if not failed:
        return
    calls = Counter(f"{label} ({code})" for label, code in failed)
    reason = "API 失敗重試用盡: " + "、".join(f"{call} {n} 次" for call, n in calls.items())
    context.note(day, reason + "; 本日作廢 (重播不採用退回粗分排序或略過篩檢的結果)")
    for market_day in days:
        market_day.invalid_reason = market_day.invalid_reason or reason


def _skipped_day(day: date, reason: str) -> list[MarketDay]:
    return [
        MarketDay(
            edition_date=day.isoformat(),
            market=market,
            selection_mode="skipped",
            candidates=[],
            picks=[],
            notes=[f"未重播: {reason}"],
            invalid_reason=reason,
        )
        for market in MARKET_CODES
    ]


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
    if options.rep != 1 or options.triage_rep != 1:
        notes.append(
            f"重跑: 組稿與目標重判的模型呼叫為第 {options.rep} 次抽樣、完整初篩為第"
            f" {options.triage_rep} 次抽樣 (快取鍵含抽樣編號; 第 1 次沿用既有快取)。"
            "DeepSeek API 沒有隨機種子參數, 無法固定抽樣, 只能固定輸入。"
        )
    if options.stages == "retriage":
        notes.append(
            f"retriage: 先以 {options.base_variant.name if options.base_variant else '?'}"
            " 的 triage 重播 (完整初篩第 1 次抽樣, 快取) 重建事件分群,"
            f" 再以 {options.variant.name} 的 triage prompt 只重判選中的文章"
            " (與 base 呼叫完全相同的文章與候選事件輸入), 更新 impact、escalation、"
            "story_type、new_facts、event_date 與分數; topic、directness 與事件歸屬不變。"
            f" 選取條件: {options.selection.describe()}。"
            " base 判為 relevant 的文章才可能被選; 因重判跌破 relevant 的文章退出事件,"
            " base 判為不 relevant、新 prompt 下可能達標的文章不會被加入 (近似)。"
        )
        if options.via_variant is not None:
            notes.append(
                f"串接: 先以 {options.via_variant.name} 的 triage prompt 重判能源/航運目標"
                f" (rep {options.via_rep}, 已快取者免費), 本 variant 再依「{options.select_mode}」"
                "選取重判 (pool = 各市場前 40 名事件的 relevant 文章; none = 不重判, 只重跑組稿)。"
            )
        if options.variant.event_date_rule:
            notes.append(
                "event_date 規則的近似: 重判無法重開事件, 因此事件內所有 relevant 文章的"
                " event_date 都過舊時, 視為該事件不會被開出 (文章移出事件、不再 relevant);"
                " 只有部分文章過舊的事件保留, 過舊文章以無新事實計分。"
            )
        if options.holdout is not None:
            notes.append(
                f"保留期 {options.holdout[0]}~{options.holdout[1]} 的已上架版面只當前版篩檢的輸入;"
                " 對到它們的決策只記日期, 不記標題。"
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
    name = f"{stamp}-{options.variant.name}-{options.stages}"
    if options.rep != 1 or options.triage_rep != 1:
        name += f"-rep{options.rep}" + (
            f"-trep{options.triage_rep}" if options.triage_rep != 1 else ""
        )
    if options.dry_run:
        name += "-dryrun"
    base = environment.runs_dir / name
    path, suffix = base, 2
    while path.exists():
        path, suffix = Path(f"{base}-{suffix}"), suffix + 1
    return path


async def run(environment: ReplayEnvironment, options: RunOptions) -> Path:
    """Replay ``options.snapshot`` and write its run directory; returns the directory."""
    if not 1 <= options.concurrency <= MAX_CONCURRENCY:
        raise ReplayError(f"--concurrency must be between 1 and {MAX_CONCURRENCY}")
    if options.stages == "retriage" and options.base_variant is None:
        raise ReplayError("a retriage run needs --base-variant")
    if options.dry_run and options.stages != "retriage":
        raise ReplayError("--dry-run only applies to a retriage run")
    # Nothing a replay starts may reach Slack, whatever the shell exported.
    os.environ.pop(SLACK_ENVIRONMENT, None)
    metadata = snapshot.prepare(environment, options.snapshot)
    reference = snapshot.load_reference(environment, options.snapshot)
    started = datetime.now(UTC)
    run_dir = _run_name(environment, options, started)
    ledger = Ledger(environment.ledger_path(options.round), run_dir.name)
    database = f"run_{started:%Y%m%d%H%M%S}_{options.variant.name}_{options.stages}"
    snapshot.create_database(environment, database, template=options.snapshot.template)
    try:
        snapshot.migrate(environment, database)
        settings = _settings(environment, options, database)
        engine = create_engine(settings)
        stats = CacheStats()
        gate_holder: list[TriageGate | None] = [None]
        failures = ApiFailures()
        runtime = _runtime(
            environment, options, settings, engine, gate_holder, stats, ledger, failures
        )
        context = _Context(environment, options, runtime, engine, reference, gate_holder)
        context.stats = stats
        if options.stages == "retriage":
            context.recorder = retriage.RecordingModel(runtime.llm)
            context.base_runtime = replace(
                runtime,
                settings=settings.model_copy(
                    update={"newsroom_selection_variant": context.base_variant.name}
                ),
                llm=context.recorder,
            )
        days: list[MarketDay] = []
        try:
            for day in options.snapshot.dates():
                logger.info("newsroom.replay_day", extra={"edition_date": day.isoformat()})
                failed_before = len(failures.calls)
                if day in options.skipped:
                    days += _skipped_day(day, options.skipped[day])
                    context.note(day, f"未重播, 標為作廢: {options.skipped[day]}")
                    continue
                if options.stages == "score":
                    days += await _score_day(context, day)
                    continue
                if options.stages == "retriage":
                    if not await _retriage_day(context, day):
                        continue
                elif options.stages == "triage":
                    with replay_rep(options.triage_rep):
                        await _triage_window(context, day)
                else:
                    await _rescore(context, day)
                with replay_rep(options.rep):
                    assembled = await _assemble_day(context, day)
                _void_failed_calls(context, day, assembled, failures.calls[failed_before:])
                days += assembled
        finally:
            await runtime.aclose()
            await engine.dispose()
    finally:
        if not options.keep_database:
            snapshot.drop_database(environment, database)
    round_total = ledger_total(read_ledger(ledger.path))
    meta = {
        "start": options.snapshot.start.isoformat(),
        "end": options.snapshot.end.isoformat(),
        "variant": options.variant.name,
        "stages": options.stages,
        "base_variant": options.base_variant.name if options.base_variant else None,
        "rep": options.rep,
        "triage_rep": options.triage_rep,
        "selection": options.selection.describe() if options.stages == "retriage" else None,
        "via_variant": options.via_variant.name if options.via_variant else None,
        "via_rep": options.via_rep,
        "select_mode": options.select_mode if options.stages == "retriage" else None,
        "skipped": {day.isoformat(): reason for day, reason in options.skip_days},
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
        "round": options.round,
        "round_usd": round(round_total["usd"], 6),
        "notes": _notes(options),
        "day_notes": context.day_notes,
        "tracks": [
            {"label": track.label, "markets": list(track.markets), "pattern": track.pattern}
            for track in options.tracks
        ],
    }
    if options.stages == "retriage":
        meta["retriage_estimate"] = {
            **context.estimate.as_dict(),
            "note": "dry run: 只估算, 沒有呼叫"
            if options.dry_run
            else "執行前估算 (實際花費見 cache)",
        }
    if options.dry_run:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return run_dir
    write_run(run_dir, meta, days)
    for name, rows in (("decisions.json", context.decisions), ("blocked.json", context.blocked)):
        if rows:
            (run_dir / name).write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    if context.retriaged:
        (run_dir / "retriage.json").write_text(
            json.dumps(
                sorted(
                    context.retriaged,
                    key=lambda row: (row["edition_date"], row["step"], row["article_id"]),
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
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
