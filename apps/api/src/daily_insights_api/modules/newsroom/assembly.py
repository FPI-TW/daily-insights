"""Workstream ③: 08:00 edition assembly — scoring, editor pass, quotas (spec §6.3).

Owner: editions worktree. Triggered by the ``newsroom_assemble`` orchestration
function; must be idempotent per edition date.

Assembly is not a queue stage, so the editor call gets a small bounded retry
here, using the same retryable/fatal split as the queue (spec §5). Exhausted
retries fall back to score order (D5).
"""

import asyncio
import html
import logging
import math
import re
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom import clock, events_service, queue
from daily_insights_api.modules.newsroom.contracts import (
    DuplicateGroups,
    EditorResult,
    PublishedMatches,
)
from daily_insights_api.modules.newsroom.models import (
    EMBEDDING_DIMENSIONS,
    MARKET_CODES,
    NewsroomArticle,
    NewsroomDedupeDecision,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEditorRating,
    NewsroomEvent,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.notifier import Notice
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.queue import FatalStageError, RetryableStageError
from daily_insights_api.modules.newsroom.scoring import STORY_TYPE_CAP, UNSELECTABLE_STORY_TYPES
from daily_insights_api.modules.newsroom.translation import contract_error, load_prompt
from daily_insights_api.modules.newsroom.variants import SelectionVariant, get_variant
from daily_insights_api.modules.newsroom.worker import Runtime

logger = logging.getLogger(__name__)

# Version per editor prompt file (``SelectionVariant.editor_prompt``), written to
# the LLM audit and to every stored rating.
EDITOR_PROMPT_VERSIONS: Mapping[str, str] = {
    "editor_v3": "newsroom.editor.v3",
    "editor": "newsroom.editor.v4",
    "editor_p0b": "newsroom.editor.v5",
}
# Editor prompt v4 asks for a reason when the stars and the event's top triage
# impact differ by at least this much.
REASON_STAR_GAP = 2
DEDUPE_PROMPT_VERSION = "newsroom.dedupe.v2"
REPEAT_PROMPT_VERSION = "newsroom.repeat.v1"
DEDUPE_HEADLINES = 3
# Earlier editions a candidate is compared with before it may be selected again (D14).
PUBLISHED_LOOKBACK_DAYS = 2
# The model alone over-groups related stories (same company, same theme); a
# match also needs near-identical working titles. Measured on real windows:
# true repeats scored 0.90-0.99, the model's false groups 0.47-0.69, and a
# preview against its result 0.77-0.83. Replaying 2026-10-07..15, most proposals
# between 0.75 and 0.85 were true repeats; the preview/result pairs among them
# carry different figures, so in that band the titles must share a key figure.
DEDUPE_TITLE_SIMILARITY = 0.85
DEDUPE_FIGURE_SIMILARITY = 0.75
# One story told from several angles (an earnings call's revenue, guidance and
# capex pieces) has titles too different for the bars above, but its events'
# mean article embeddings stay close. Replaying 135 proposals of 2026-10-03..16:
# the angles of one earnings release scored 0.90-0.94; the nearest false pair,
# a "market awaits the call" preview against the results, 0.873; other stories
# of the same company or theme 0.85 and below.
DEDUPE_ARTICLE_SIMILARITY = 0.88
# An amount or rate in a title: two or more digits, or a decimal. A number
# followed by a date unit (year, month, day, quarter, week) is not one.
_KEY_FIGURE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])(?!\s*[年月日季週周])")
# Candidates per market the editor rates. Rating ranks 31-60 of 2026-10-07..15
# found no 5-star and 22 4-star events in 810, 15 of them at ranks 31-40 (about
# half were split duplicates or stories another market already carried).
CANDIDATE_LIMIT = 40
HEADLINES_PER_EVENT = 5
FIVE_STAR_UNLIMITED = 5
FOUR_STAR_LIMIT = 5
FILL_TARGET = 5
FILLER_STARS = 3
# Picks per story group: events the duplicate screen grouped but did not merge.
# On 2026-10-16 it grouped 18 TSMC earnings-call events whose titles differ too
# much to merge (similarity 0.26-0.74), and five of them filled the tw_equity
# edition. The model also over-groups related stories (a preview with its
# result, another TSMC fab), so a group keeps two picks rather than one.
STORY_GROUP_LIMIT = 2
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

    # The publisher (source name): one outlet's several feeds count as one source.
    outlet: str
    market_score: float
    weight: float
    # The story type's ceiling, applied after the source weight so a weighted
    # source cannot lift an opinion or preview piece past it.
    cap: float = math.inf


def distinct_outlets(signals: Sequence[ArticleSignal]) -> int:
    """The ``n`` of the multi-source bonus: distinct publishers among ``signals``.

    One outlet's several feeds count once. Outlets rewriting one another still
    count separately; independent sources are a planned replacement (P1).
    """
    return len({signal.outlet for signal in signals})


def source_bonus(n: int, market: str, *, variant: SelectionVariant) -> float:
    """Points for ``n`` outlets reporting an event in ``market``; one outlet earns none."""
    if n <= 1:
        return 0.0
    step = variant.source_bonus_step[market]
    cap = variant.source_bonus_cap[market]
    if variant.source_bonus_mode == "linear":
        return min(step * (n - 1), cap)
    return min(step * math.log2(n), cap)


def clamp_weight(weight: float, *, variant: SelectionVariant) -> float:
    """``weight`` held inside the variant's range, so no source dominates on weight alone."""
    if variant.weight_clamp is None:
        return weight
    low, high = variant.weight_clamp
    return min(max(weight, low), high)


def event_score(
    signals: Sequence[ArticleSignal], market: str, *, variant: SelectionVariant
) -> float:
    """``max(min(score * clamped source weight, cap))`` plus the multi-source bonus."""
    if not signals:
        return 0.0
    best = max(
        min(signal.market_score * clamp_weight(signal.weight, variant=variant), signal.cap)
        for signal in signals
    )
    return best + source_bonus(distinct_outlets(signals), market, variant=variant)


@dataclass(frozen=True, slots=True)
class Rated:
    event_id: uuid.UUID
    stars: int
    score: float
    # Place in the ranked candidate list; settles equal stars and score.
    position: int = 0


def _quota_order(rated: Rated) -> tuple[int, float, int, str]:
    return (-rated.stars, -rated.score, rated.position, str(rated.event_id))


StoryGroups = Mapping[uuid.UUID, int]


def _within_story_limit[T](
    ordered: Sequence[T], event_id: Callable[[T], uuid.UUID], story_groups: StoryGroups
) -> list[T]:
    """``ordered`` without the picks past ``STORY_GROUP_LIMIT`` of any story group."""
    taken: Counter[int] = Counter()
    kept: list[T] = []
    for item in ordered:
        group = story_groups.get(event_id(item))
        if group is not None:
            if taken[group] >= STORY_GROUP_LIMIT:
                continue
            taken[group] += 1
        kept.append(item)
    return kept


def apply_quota(rated: Sequence[Rated], story_groups: StoryGroups | None = None) -> list[Rated]:
    """D8 quotas, applied by code rather than the model.

    Every 5-star event is kept; at most five 4-star events; only when 5 + 4
    stars total fewer than five do 3-star events fill up to five items. 1-2
    star events never fill, so a thin day publishes fewer than five. Ordered by
    stars, then score, descending. Each story group keeps its best
    ``STORY_GROUP_LIMIT`` events before the quotas apply.
    """
    ordered = _within_story_limit(
        sorted(rated, key=_quota_order), lambda item: item.event_id, story_groups or {}
    )
    five = [item for item in ordered if item.stars == 5]
    four = [item for item in ordered if item.stars == 4][:FOUR_STAR_LIMIT]
    chosen = five + four
    if len(chosen) < FILL_TARGET:
        fillers = [item for item in ordered if item.stars == FILLER_STARS]
        chosen += fillers[: FILL_TARGET - len(chosen)]
    return sorted(chosen, key=_quota_order)


def needs_five_star_review(five_star_count: int, *, variant: SelectionVariant) -> bool:
    """More visible 5-star picks in one market edition than the variant's soft cap.

    The quota still keeps every 5-star event; this only asks a person to check
    the ratings, since a day rarely carries more than a few market-wide stories.
    """
    cap = variant.five_star_soft_cap
    return cap is not None and five_star_count > cap


def fallback_selection(
    scores: Sequence[tuple[uuid.UUID, float]], story_groups: StoryGroups | None = None
) -> list[tuple[uuid.UUID, float]]:
    """Editor unavailable: the top five events by score (D5), ties in the given order.

    Story groups are limited as in ``apply_quota``.
    """
    ordered = sorted(scores, key=lambda pair: -pair[1])
    return _within_story_limit(ordered, lambda pair: pair[0], story_groups or {})[:FALLBACK_LIMIT]


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


def editor_prompt_version(*, variant: SelectionVariant) -> str:
    return EDITOR_PROMPT_VERSIONS[variant.editor_prompt]


def dedupe_prompt(*, variant: SelectionVariant) -> tuple[str, str]:
    """The duplicate screen's prompt text with the variant's addenda, and its audit version."""
    parts = (variant.dedupe_prompt, *variant.dedupe_prompt_addenda)
    text = "\n\n".join(load_prompt(part) for part in parts)
    version = "+".join((DEDUPE_PROMPT_VERSION, *variant.dedupe_prompt_addenda))
    return text, version


# --- Candidate loading -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """An event eligible for one market; counts cover its selectable reports only."""

    event_id: uuid.UUID
    working_title: str
    score: float
    headlines: tuple[tuple[str, str], ...]
    article_count: int
    source_count: int
    trust_tier: int
    first_seen_at: datetime


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
    story_type: str | None
    first_seen_at: datetime
    # Read by the extended editor input only (``SelectionVariant.editor_extended_input``).
    feed_summary: str | None = None
    # Triage's raw judgements; ``None`` / 0 when a stored signal is missing or malformed.
    impact: int | None = None
    escalation: int = 0


def _signal(signals: Any, key: str, low: int, high: int) -> int | None:
    """An integer triage signal within ``[low, high]``, or ``None``."""
    value = signals.get(key) if isinstance(signals, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    return None


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
                NewsroomArticle.story_type,
                NewsroomArticle.first_seen_at,
                NewsroomArticle.feed_summary,
                NewsroomArticle.triage_signals,
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
    for (
        event_id,
        working_title,
        title,
        source_id,
        name,
        weight,
        tier,
        scores,
        body,
        story_type,
        first_seen_at,
        feed_summary,
        signals,
    ) in rows:
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
                story_type=story_type,
                first_seen_at=first_seen_at,
                feed_summary=feed_summary,
                impact=_signal(signals, "impact", 1, 5),
                # Signals stored before triage v3 carry no escalation: none.
                escalation=_signal(signals, "escalation", 0, 2) or 0,
            )
        )
    return titles, articles


def _market_score(value: Any) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _signals(rows: Sequence[_ArticleRow], market: str) -> list[ArticleSignal]:
    return [
        ArticleSignal(
            row.source_name,
            _market_score(row.market_scores.get(market)),
            row.weight,
            STORY_TYPE_CAP.get(row.story_type or "", math.inf),
        )
        for row in rows
    ]


async def event_scores(
    database: AsyncSession, edition_date: date, market: str, *, variant: SelectionVariant
) -> dict[uuid.UUID, float]:
    """``market``'s score (spec §6.3 step 2) for every open event of the window.

    Covers each event with at least one relevant article, whether or not it has
    a full text or a positive score; the admin console ranks its candidate list
    with this, while assembly additionally filters (``rank_candidates``).
    """
    _, articles = await _load_event_articles(database, edition_date)
    return {
        event_id: event_score(_signals(rows, market), market, variant=variant)
        for event_id, rows in articles.items()
    }


def _candidate_order(candidate: Candidate) -> tuple[float, int, int, int, datetime, str]:
    """Score first; equal scores go to the better-corroborated, then the earlier, story."""
    return (
        -candidate.score,
        -candidate.source_count,
        -candidate.article_count,
        -candidate.trust_tier,
        candidate.first_seen_at,
        str(candidate.event_id),
    )


def rank_candidates(
    titles: dict[uuid.UUID, str],
    articles: dict[uuid.UUID, list[_ArticleRow]],
    market: str,
    *,
    excluded: Collection[uuid.UUID] = (),
    variant: SelectionVariant,
) -> list[Candidate]:
    """Eligible events for ``market`` by score: relevant, scored, with a full text.

    Price-action pieces neither score nor count: an event made only of them is
    not a candidate (the morning report already carries market moves), and one
    that also has the underlying news is ranked on that news alone. ``excluded``
    events (already published by an earlier edition) are left out.
    """
    candidates: list[Candidate] = []
    for event_id, rows in articles.items():
        if event_id in excluded or not any(row.has_body for row in rows):
            continue
        selectable = [row for row in rows if row.story_type not in UNSELECTABLE_STORY_TYPES]
        signals = _signals(selectable, market)
        if not signals or max(signal.market_score for signal in signals) <= 0:
            continue
        best_first = sorted(selectable, key=lambda row: (-row.trust_tier, -row.weight))
        candidates.append(
            Candidate(
                event_id=event_id,
                working_title=titles[event_id],
                score=event_score(signals, market, variant=variant),
                headlines=tuple(
                    (row.title, row.source_name) for row in best_first[:HEADLINES_PER_EVENT]
                ),
                article_count=len(selectable),
                # Count outlets by publisher name so CNBC/Guardian/鉅亨 multi-feeds
                # do not inflate unique-source weight for one house.
                source_count=len({row.source_name for row in selectable}),
                trust_tier=max(row.trust_tier for row in selectable),
                first_seen_at=min(row.first_seen_at for row in selectable),
            )
        )
    candidates.sort(key=_candidate_order)
    return candidates[:CANDIDATE_LIMIT]


def _candidate_union(
    titles: dict[uuid.UUID, str],
    articles: dict[uuid.UUID, list[_ArticleRow]],
    excluded: Collection[uuid.UUID] = (),
    *,
    variant: SelectionVariant,
) -> dict[uuid.UUID, Candidate]:
    """Every market's candidates, each event once at its best score."""
    union: dict[uuid.UUID, Candidate] = {}
    for market in MARKET_CODES:
        for candidate in rank_candidates(
            titles, articles, market, excluded=excluded, variant=variant
        ):
            kept = union.get(candidate.event_id)
            if kept is None or candidate.score > kept.score:
                union[candidate.event_id] = candidate
    return union


# --- Duplicate merge --------------------------------------------------------------


def validate_duplicate_groups(result: DuplicateGroups, known: set[str]) -> list[list[str]]:
    """Usable groups only: known ids, each in one group, two or more per group.

    The merge is best effort, so unknown or repeated ids are dropped rather than
    failing the assembly.
    """
    seen: set[str] = set()
    groups: list[list[str]] = []
    for group in result.groups:
        ids = [
            event_id
            for event_id in dict.fromkeys(item.strip() for item in group)
            if event_id in known and event_id not in seen
        ]
        if len(ids) >= 2:
            groups.append(ids)
            seen.update(ids)
    return groups


def _dedupe_payload(candidates: Sequence[Candidate]) -> dict[str, Any]:
    return {
        "events": [
            {
                "event_id": str(candidate.event_id),
                "working_title": candidate.working_title,
                "headlines": [title for title, _ in candidate.headlines[:DEDUPE_HEADLINES]],
            }
            for candidate in candidates
        ]
    }


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def key_figures(title: str) -> frozenset[str]:
    """Amounts and rates in a title (``5,118`` reads as ``5118``), dates left out."""
    plain = re.sub(r"(?<=\d),(?=\d{3})", "", title)
    return frozenset(
        figure for figure in _KEY_FIGURE.findall(plain) if "." in figure or len(figure) >= 2
    )


def same_story_titles(similarity: float, title: str, other: str) -> bool:
    """Whether two working titles the model grouped are close enough to act on."""
    if similarity >= DEDUPE_TITLE_SIMILARITY:
        return True
    return similarity >= DEDUPE_FIGURE_SIMILARITY and bool(key_figures(title) & key_figures(other))


async def article_similarities(
    database: AsyncSession, target: uuid.UUID, others: Collection[uuid.UUID]
) -> dict[uuid.UUID, float]:
    """Cosine similarity of each of ``others``' mean article embedding to ``target``'s.

    Events without an embedded article are left out.
    """
    centroids = (
        select(
            NewsroomArticle.event_id.label("event_id"),
            func.avg(NewsroomArticle.embedding, type_=Vector(EMBEDDING_DIMENSIONS)).label(
                "centroid"
            ),
        )
        .where(
            NewsroomArticle.event_id.in_([target, *others]),
            NewsroomArticle.embedding.is_not(None),
        )
        .group_by(NewsroomArticle.event_id)
        .cte("centroids")
    )
    base, other = centroids.alias("base"), centroids.alias("other")
    rows = await database.execute(
        select(other.c.event_id, 1 - other.c.centroid.cosine_distance(base.c.centroid))
        .select_from(base.join(other, other.c.event_id != base.c.event_id))
        .where(base.c.event_id == target)
    )
    return {event_id: float(similarity) for event_id, similarity in rows}


def same_story(
    title_similarity: float, article_similarity: float | None, title: str, other: str
) -> bool:
    """Whether two events the model grouped are one story: titles or articles match."""
    return same_story_titles(title_similarity, title, other) or (
        article_similarity is not None and article_similarity >= DEDUPE_ARTICLE_SIMILARITY
    )


def _decision(
    edition_date: date,
    scope: str,
    *,
    event: tuple[uuid.UUID, str],
    matched: tuple[uuid.UUID, str],
    similarity: float | None,
    outcome: str,
    new_facts: bool | None = None,
    article_similarity: float | None = None,
) -> NewsroomDedupeDecision:
    return NewsroomDedupeDecision(
        edition_date=edition_date,
        scope=scope,
        event_id=event[0],
        event_title=event[1][:500],
        matched_event_id=matched[0],
        matched_title=matched[1][:500],
        title_similarity=similarity,
        article_similarity=article_similarity,
        threshold=DEDUPE_TITLE_SIMILARITY,
        shared_figure=bool(key_figures(event[1]) & key_figures(matched[1])),
        new_facts=new_facts,
        outcome=outcome,
    )


async def _settle_failed_call(
    database: AsyncSession, error: RetryableStageError | FatalStageError, event: str
) -> None:
    """Keep the failed call's audit rows; the screens are best effort and never block assembly."""
    await database.rollback()
    database.add_all(error.audit_rows)
    await database.commit()
    logger.warning(event, extra={"error_code": error.code})


@dataclass(frozen=True, slots=True)
class DuplicateScreen:
    """What the duplicate screen did: events merged away, and the groups left open.

    ``story_groups`` maps each still-open event of a group with two or more of
    them to the group's index; selection limits the picks per group.
    """

    merged: int = 0
    story_groups: dict[uuid.UUID, int] = field(default_factory=dict)


async def merge_duplicate_candidates(
    runtime: Runtime,
    edition_date: date,
    titles: dict[uuid.UUID, str],
    articles: dict[uuid.UUID, list[_ArticleRow]],
) -> DuplicateScreen:
    """Merge candidates that triage split from one story.

    Per-article triage sometimes opens several events for one big story (similar
    "market awaits X" events crowd the real one out of its nearest candidates),
    which spends quota slots on repeats. One model call over every market's
    candidates proposes groups; an event joins its group's best-covered event
    only when their working titles or their articles also match (``same_story``).
    Every proposed pair is recorded with both similarities, merged or not. The events of a group
    that stay open (one story told from several angles, or related stories the
    model over-grouped) are returned as a story group.
    """
    variant = get_variant(runtime.settings.newsroom_selection_variant)
    union = _candidate_union(titles, articles, variant=variant)
    if len(union) < 2:
        return DuplicateScreen()
    event_ids = list(union)
    system, prompt_version = dedupe_prompt(variant=variant)
    async with runtime.session_factory() as database:
        try:
            result = await runtime.llm.complete(
                database,
                model=runtime.settings.newsroom_editor_model,
                system=system,
                payload=_dedupe_payload(list(union.values())),
                result_type=DuplicateGroups,
                audit=CallAudit("editor", None, prompt_version),
            )
            groups = validate_duplicate_groups(result, {str(event_id) for event_id in union})
            vectors = (
                dict(
                    zip(
                        event_ids,
                        await runtime.embedder.embed(
                            database,
                            [titles[event_id] for event_id in event_ids],
                            audit=CallAudit("embed", None, prompt_version),
                        ),
                        strict=True,
                    )
                )
                if groups
                else {}
            )
        except (RetryableStageError, FatalStageError) as error:
            await _settle_failed_call(database, error, "newsroom.dedupe_failed")
            return DuplicateScreen()
        merged = 0
        story_groups: dict[uuid.UUID, int] = {}
        for index, group in enumerate(groups):
            ids = [uuid.UUID(event_id) for event_id in group]
            target = max(ids, key=lambda event_id: (len(articles[event_id]), union[event_id].score))
            similarity = {
                event_id: _cosine(vectors[target], vectors[event_id])
                for event_id in ids
                if event_id != target
            }
            closeness = await article_similarities(database, target, list(similarity))
            sources = [
                event_id
                for event_id, value in similarity.items()
                if same_story(value, closeness.get(event_id), titles[event_id], titles[target])
            ]
            outcome = "merged"
            if sources:
                try:
                    async with database.begin_nested():
                        await events_service.merge_events(
                            database, target_id=target, source_ids=sources, user_id=None
                        )
                except events_service.EventServiceError as error:
                    logger.warning(
                        "newsroom.dedupe_merge_skipped",
                        extra={"target_event_id": str(target), "error_code": error.code},
                    )
                    outcome = "merge_failed"
                else:
                    merged += len(sources)
            remaining = [
                event_id for event_id in ids if outcome != "merged" or event_id not in sources
            ]
            if len(remaining) >= 2:
                story_groups.update(dict.fromkeys(remaining, index))
            database.add_all(
                _decision(
                    edition_date,
                    "window",
                    event=(event_id, titles[event_id]),
                    matched=(target, titles[target]),
                    similarity=value,
                    article_similarity=closeness.get(event_id),
                    outcome=outcome if event_id in sources else "below_threshold",
                )
                for event_id, value in similarity.items()
            )
        await database.commit()
    return DuplicateScreen(merged, story_groups)


# --- Stories an earlier edition already carried (D14) -------------------------------


@dataclass(frozen=True, slots=True)
class PublishedEvent:
    """A visible item of a recent published edition, one row per event."""

    event_id: uuid.UUID
    edition_date: date
    working_title: str
    headline: str
    summary: str


@dataclass(frozen=True, slots=True)
class RepeatScreen:
    """Candidates an earlier edition already carried.

    ``excluded`` repeat a published story with nothing new and are not selected
    again; ``follow_ups`` may add to one and reach the editor with that earlier
    headline attached.
    """

    excluded: frozenset[uuid.UUID] = frozenset()
    follow_ups: dict[uuid.UUID, PublishedEvent] = field(default_factory=dict)


async def recently_published(database: AsyncSession, edition_date: date) -> list[PublishedEvent]:
    """Events readers saw in the editions of the previous ``PUBLISHED_LOOKBACK_DAYS`` days."""
    rows = (
        await database.execute(
            select(
                NewsroomEvent.id,
                NewsroomEdition.edition_date,
                NewsroomEvent.working_title,
                NewsroomEvent.headline_zh_hant,
                NewsroomEvent.summary_zh_hant,
            )
            .join(NewsroomEditionItem, NewsroomEditionItem.event_id == NewsroomEvent.id)
            .join(NewsroomEdition, NewsroomEdition.id == NewsroomEditionItem.edition_id)
            .where(
                NewsroomEdition.status == "published",
                NewsroomEdition.edition_date < edition_date,
                NewsroomEdition.edition_date
                >= edition_date - timedelta(days=PUBLISHED_LOOKBACK_DAYS),
                NewsroomEditionItem.removed_at.is_(None),
                NewsroomEditionItem.hidden_at.is_(None),
                NewsroomEditionItem.abandoned_at.is_(None),
                NewsroomEvent.headline_zh_hant.is_not(None),
            )
            .order_by(NewsroomEdition.edition_date.desc(), NewsroomEvent.id)
        )
    ).all()
    published: dict[uuid.UUID, PublishedEvent] = {}
    for event_id, published_on, working_title, headline, summary in rows:
        published.setdefault(
            event_id,
            PublishedEvent(event_id, published_on, working_title, headline, summary or ""),
        )
    return list(published.values())


def validate_published_matches(
    result: PublishedMatches, candidates: set[str], published: set[str]
) -> list[tuple[str, str, bool]]:
    """Usable matches only: known ids, each candidate matched at most once."""
    seen: set[str] = set()
    matches: list[tuple[str, str, bool]] = []
    for match in result.matches:
        event_id, published_id = match.event_id.strip(), match.published_id.strip()
        if event_id in candidates and published_id in published and event_id not in seen:
            seen.add(event_id)
            matches.append((event_id, published_id, match.new_facts))
    return matches


def _repeat_payload(
    published: Sequence[PublishedEvent], candidates: Sequence[Candidate]
) -> dict[str, Any]:
    return {
        "published": [
            {
                "id": str(item.event_id),
                "edition_date": item.edition_date.isoformat(),
                "headline": item.headline,
                "summary": item.summary,
            }
            for item in published
        ],
        "candidates": [
            {
                "event_id": str(candidate.event_id),
                "working_title": candidate.working_title,
                "headlines": [title for title, _ in candidate.headlines[:DEDUPE_HEADLINES]],
            }
            for candidate in candidates
        ],
    }


async def screen_published_repeats(
    runtime: Runtime,
    edition_date: date,
    titles: dict[uuid.UUID, str],
    articles: dict[uuid.UUID, list[_ArticleRow]],
) -> RepeatScreen:
    """Compare every market's candidates with what the last editions published.

    Events live in one window (D14), so a story that resurfaces a day later is
    a new event and used to be selected again. One model call matches candidates
    to recently published events and says whether each adds new facts. A match
    without new facts whose working title also matches (``same_story_titles``) the
    published event's is excluded; any other match stays a candidate and is
    shown to the editor as a follow-up. Every match is recorded.
    """
    union = _candidate_union(
        titles, articles, variant=get_variant(runtime.settings.newsroom_selection_variant)
    )
    if not union:
        return RepeatScreen()
    async with runtime.session_factory() as database:
        published = {
            item.event_id: item for item in await recently_published(database, edition_date)
        }
        if not published:
            return RepeatScreen()
        try:
            result = await runtime.llm.complete(
                database,
                model=runtime.settings.newsroom_editor_model,
                system=load_prompt("repeat"),
                payload=_repeat_payload(list(published.values()), list(union.values())),
                result_type=PublishedMatches,
                audit=CallAudit("editor", None, REPEAT_PROMPT_VERSION),
            )
            matches = [
                (uuid.UUID(event_id), uuid.UUID(published_id), new_facts)
                for event_id, published_id, new_facts in validate_published_matches(
                    result,
                    {str(event_id) for event_id in union},
                    {str(event_id) for event_id in published},
                )
            ]
            texts = list(
                dict.fromkeys(
                    title
                    for event_id, published_id, _ in matches
                    for title in (titles[event_id], published[published_id].working_title)
                )
            )
            vectors = (
                dict(
                    zip(
                        texts,
                        await runtime.embedder.embed(
                            database, texts, audit=CallAudit("embed", None, REPEAT_PROMPT_VERSION)
                        ),
                        strict=True,
                    )
                )
                if texts
                else {}
            )
        except (RetryableStageError, FatalStageError) as error:
            await _settle_failed_call(database, error, "newsroom.repeat_screen_failed")
            return RepeatScreen()
        excluded: set[uuid.UUID] = set()
        follow_ups: dict[uuid.UUID, PublishedEvent] = {}
        for event_id, published_id, new_facts in matches:
            earlier = published[published_id]
            similarity = _cosine(vectors[titles[event_id]], vectors[earlier.working_title])
            repeat = not new_facts and same_story_titles(
                similarity, titles[event_id], earlier.working_title
            )
            if repeat:
                excluded.add(event_id)
            else:
                follow_ups[event_id] = earlier
            database.add(
                _decision(
                    edition_date,
                    "published",
                    event=(event_id, titles[event_id]),
                    matched=(published_id, earlier.working_title),
                    similarity=similarity,
                    outcome="excluded" if repeat else "follow_up",
                    new_facts=new_facts,
                )
            )
        await database.commit()
    return RepeatScreen(frozenset(excluded), follow_ups)


# --- Editor pass -----------------------------------------------------------------

# The extended editor input's feed summary: one or two sentences, about this long.
EDITOR_SUMMARY_CHARS = 150
# Sentence ends: the CJK full stop and full-width or ASCII ! and ?; a period only
# after a lowercase letter or digit and before a space or the end, so "U.S." does
# not end a sentence.
_SENTENCE_END = re.compile(r"[\u3002\uff01\uff1f!?]|(?<=[a-z0-9])\.(?=\s|$)")


def _story_labels(
    candidates: Sequence[Candidate], story_groups: StoryGroups
) -> dict[uuid.UUID, str]:
    """``g1``, ``g2``... for the story groups with two or more of ``candidates``."""
    members = Counter(
        story_groups[candidate.event_id]
        for candidate in candidates
        if candidate.event_id in story_groups
    )
    labels: dict[int, str] = {}
    for candidate in candidates:
        group = story_groups.get(candidate.event_id)
        if group is not None and members[group] >= 2 and group not in labels:
            labels[group] = f"g{len(labels) + 1}"
    return {
        candidate.event_id: labels[story_groups[candidate.event_id]]
        for candidate in candidates
        if story_groups.get(candidate.event_id) in labels
    }


def lead_summary(text: str | None, title: str = "") -> str | None:
    """A feed summary cut to its first two sentences and about ``EDITOR_SUMMARY_CHARS``.

    Some feeds leave HTML entities in their summaries; they are decoded first. A
    summary that only repeats the headline says nothing more, so it counts as none.
    """
    squashed = " ".join(html.unescape(text or "").split())
    if not squashed or squashed == " ".join(title.split()):
        return None
    ends = [match.end() for match in _SENTENCE_END.finditer(squashed)]
    if len(ends) > 2:
        squashed = squashed[: ends[1]]
    if len(squashed) <= EDITOR_SUMMARY_CHARS:
        return squashed
    # Prefer ending on a full sentence unless that leaves only a fragment.
    cut = max((end for end in ends if end <= EDITOR_SUMMARY_CHARS), default=0)
    if cut >= EDITOR_SUMMARY_CHARS // 3:
        return squashed[:cut]
    return squashed[:EDITOR_SUMMARY_CHARS].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class EventBrief:
    """What the extended editor input adds to an event, from its selectable reports."""

    max_impact: int | None
    max_escalation: int
    main_story_type: str | None
    lead_summary: str | None


def event_brief(rows: Sequence[_ArticleRow], headlines: Sequence[tuple[str, str]]) -> EventBrief:
    """Top impact and escalation, the commonest story type and the lead headline's summary.

    Price-action pieces are left out as in ``rank_candidates``. The main story
    type is the one with the most reports, ties going to the higher impact and
    then to the earlier report; the summary is that of the first headline
    shown (in ``headlines`` order) whose report has one.
    """
    selectable = [row for row in rows if row.story_type not in UNSELECTABLE_STORY_TYPES]
    impacts = [row.impact for row in selectable if row.impact is not None]
    counts: Counter[str] = Counter()
    type_impact: dict[str, int] = {}
    for row in selectable:
        if row.story_type is not None:
            counts[row.story_type] += 1
            type_impact[row.story_type] = max(type_impact.get(row.story_type, 0), row.impact or 0)
    # ``counts`` keeps first-seen order, so ``min`` settles a full tie on the earlier report.
    main = min(counts, key=lambda kind: (-counts[kind], -type_impact[kind]), default=None)
    summary = next(
        (
            text
            for title, source in headlines
            for row in selectable
            if row.title == title
            and row.source_name == source
            and (text := lead_summary(row.feed_summary, title)) is not None
        ),
        None,
    )
    return EventBrief(
        max_impact=max(impacts, default=None),
        max_escalation=max((row.escalation for row in selectable), default=0),
        main_story_type=main,
        lead_summary=summary,
    )


def _editor_payload(
    market: str,
    candidates: Sequence[Candidate],
    follow_ups: dict[uuid.UUID, PublishedEvent],
    story_groups: StoryGroups,
    *,
    variant: SelectionVariant,
    articles: Mapping[uuid.UUID, Sequence[_ArticleRow]],
) -> dict[str, Any]:
    """The editor's input; the baseline variant's is exactly the eb6ad34 payload."""
    labels = _story_labels(candidates, story_groups)
    events: list[dict[str, Any]] = []
    for candidate in candidates:
        event: dict[str, Any] = {
            "event_id": str(candidate.event_id),
            "working_title": candidate.working_title,
            "report_count": candidate.article_count,
            "source_count": candidate.source_count,
        }
        brief = (
            event_brief(articles.get(candidate.event_id, ()), candidate.headlines)
            if variant.editor_extended_input
            else None
        )
        if brief is not None:
            event["max_impact"] = brief.max_impact
            event["max_escalation"] = brief.max_escalation
            event["main_story_type"] = brief.main_story_type
        event["headlines"] = [
            {"title": title, "source": source} for title, source in candidate.headlines
        ]
        if brief is not None and brief.lead_summary is not None:
            event["lead_summary"] = brief.lead_summary
        if candidate.event_id in labels:
            event["story_group"] = labels[candidate.event_id]
        earlier = follow_ups.get(candidate.event_id)
        if earlier is not None:
            event["earlier_coverage"] = {
                "edition_date": earlier.edition_date.isoformat(),
                "headline": earlier.headline,
            }
        events.append(event)
    return {"market": market, "events": events}


def missing_reasons(
    result: EditorResult,
    candidates: Sequence[Candidate],
    articles: Mapping[uuid.UUID, Sequence[_ArticleRow]],
) -> int:
    """Ratings ``REASON_STAR_GAP`` or more from their event's top impact that give no reason."""
    impacts = {
        str(candidate.event_id): event_brief(
            articles.get(candidate.event_id, ()), candidate.headlines
        ).max_impact
        for candidate in candidates
    }
    return sum(
        1
        for rating in result.ratings
        if not rating.reason
        and (impact := impacts.get(rating.event_id)) is not None
        and abs(rating.stars - impact) >= REASON_STAR_GAP
    )


@dataclass(frozen=True, slots=True)
class EditorRatings:
    stars: dict[str, int]
    # The reasons the editor gave, by event id; ratings without one are absent.
    reasons: dict[str, str] = field(default_factory=dict)


async def _rate(
    runtime: Runtime,
    market: str,
    candidates: Sequence[Candidate],
    articles: Mapping[uuid.UUID, Sequence[_ArticleRow]],
    follow_ups: dict[uuid.UUID, PublishedEvent],
    story_groups: StoryGroups,
    *,
    variant: SelectionVariant,
    sleep: Sleep,
    retry_delay: timedelta,
) -> EditorRatings | None:
    """Editor stars (and reasons) per event id, or ``None`` once retries are exhausted.

    A missing reason never fails the call: the prompt asks for one, the count of
    those left out is only logged.
    """
    expected = {str(candidate.event_id) for candidate in candidates}
    for attempt in range(1, EDITOR_ATTEMPTS + 1):
        async with runtime.session_factory() as database:
            try:
                result = await runtime.llm.complete(
                    database,
                    model=runtime.settings.newsroom_editor_model,
                    system=load_prompt(variant.editor_prompt),
                    payload=_editor_payload(
                        market,
                        candidates,
                        follow_ups,
                        story_groups,
                        variant=variant,
                        articles=articles,
                    ),
                    result_type=EditorResult,
                    audit=CallAudit("editor", None, editor_prompt_version(variant=variant)),
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
            if variant.editor_extended_input:
                missing = missing_reasons(result, candidates, articles)
                if missing:
                    logger.warning(
                        "newsroom.editor_reasons_missing",
                        extra={"market": market, "count": missing},
                    )
            return EditorRatings(
                ratings,
                {rating.event_id: rating.reason for rating in result.ratings if rating.reason},
            )
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
    merged_duplicates: int = 0
    excluded_repeats: int = 0

    @property
    def assembled(self) -> list[MarketReport]:
        return [market for market in self.markets if market.action == "assembled"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "edition_date": self.edition_date.isoformat(),
            "ignored_pending_triage": self.ignored_pending_triage,
            "merged_duplicates": self.merged_duplicates,
            "excluded_repeats": self.excluded_repeats,
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
    repeats: RepeatScreen,
    story_groups: StoryGroups,
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
    variant = get_variant(runtime.settings.newsroom_selection_variant)
    candidates = rank_candidates(
        titles, articles, market, excluded=repeats.excluded, variant=variant
    )
    by_id = {candidate.event_id: candidate for candidate in candidates}
    rated = (
        await _rate(
            runtime,
            market,
            candidates,
            articles,
            repeats.follow_ups,
            story_groups,
            variant=variant,
            sleep=sleep,
            retry_delay=retry_delay,
        )
        if candidates
        else EditorRatings({})
    )
    ratings = None if rated is None else rated.stars
    reasons = {} if rated is None else rated.reasons
    if ratings is None:
        mode = "fallback"
        picks = [
            Pick(event_id, None, score)
            for event_id, score in fallback_selection(
                [(candidate.event_id, candidate.score) for candidate in candidates], story_groups
            )
        ]
    else:
        mode = "editor"
        picks = [
            Pick(item.event_id, item.stars, item.score)
            for item in apply_quota(
                [
                    Rated(
                        candidate.event_id,
                        ratings[str(candidate.event_id)],
                        candidate.score,
                        position,
                    )
                    for position, candidate in enumerate(candidates)
                ],
                story_groups,
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
        if edition is not None and ratings:
            picked = {pick.event_id for pick in picks}
            database.add_all(
                NewsroomEditorRating(
                    edition_date=edition_date,
                    market_code=market,
                    event_id=candidate.event_id,
                    event_title=candidate.working_title[:500],
                    position=position,
                    score=candidate.score,
                    stars=ratings[str(candidate.event_id)],
                    picked=candidate.event_id in picked,
                    follow_up=candidate.event_id in repeats.follow_ups,
                    prompt_version=editor_prompt_version(variant=variant),
                    reason=reasons.get(str(candidate.event_id)),
                )
                for position, candidate in enumerate(candidates, start=1)
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
    duplicates = await merge_duplicate_candidates(runtime, edition_date, titles, articles)
    merged = duplicates.merged
    if merged:
        async with runtime.session_factory() as database:
            titles, articles = await _load_event_articles(database, edition_date)
    repeats = await screen_published_repeats(runtime, edition_date, titles, articles)
    report = AssemblyReport(
        edition_date, ignored, merged_duplicates=merged, excluded_repeats=len(repeats.excluded)
    )
    for market in MARKET_CODES:
        report.markets.append(
            await _assemble_market(
                runtime,
                edition_date,
                market,
                titles,
                articles,
                repeats,
                duplicates.story_groups,
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
        if merged:
            lines.append(f"已合併 {merged} 個重複事件")
        if repeats.excluded:
            lines.append(f"已排除 {len(repeats.excluded)} 個前版已上架且無新事實的事件")
        await runtime.notifier.send(
            Notice(
                kind="draft_ready",
                title=f"{edition_date.isoformat()} 重點新聞草稿完成",
                lines=tuple(lines),
                link=review_link(runtime, edition_date),
            )
        )
    return report
