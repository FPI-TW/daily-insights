"""News quality metrics over a range of edition dates (read-only).

Collection volume says little about what readers got, so this measures the
published side: how much the admins had to replace or rewrite, how often a
story ran twice (in one edition, or again a day or two later), how the
editor's stars fared against the admins' removals, and what the assembly's
duplicate screens proposed and did. Every figure is computed from stored rows;
nothing here calls a model or writes.

A story "ran twice" when two visible items' events have articles whose
embeddings are at least ``clustering.SAME_EVENT_SIMILARITY`` apart; the pairs
are listed so a reader can check them.
"""

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom import clock, clustering
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomDedupeDecision,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEditLog,
    NewsroomEditorRating,
    NewsroomEvent,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.triage import STALE_BEFORE_WINDOW

REPEAT_LOOKBACK_DAYS = 2


@dataclass(frozen=True, slots=True)
class CollectionDay:
    edition_date: date
    articles: int
    relevant: int
    published_before_window: int
    events: int
    multi_article_events: int
    published_events: int


@dataclass(frozen=True, slots=True)
class EditionQuality:
    edition_date: date
    market: str
    model_items: int
    removed_model_items: int
    manual_items: int
    hidden_items: int
    edited_items: int
    stars: dict[int, int]
    removed_by_stars: dict[int, int]

    @property
    def replacement_rate(self) -> float | None:
        """Model picks the admins removed or replaced by hand, per model pick."""
        if not self.model_items:
            return None
        return (self.removed_model_items + self.manual_items) / self.model_items


@dataclass(frozen=True, slots=True)
class RepeatPair:
    edition_date: date
    market: str
    title: str
    earlier_date: date
    earlier_market: str
    earlier_title: str
    similarity: float


@dataclass(frozen=True, slots=True)
class UnpickedCandidate:
    edition_date: date
    market: str
    position: int
    stars: int
    title: str


@dataclass
class QualityReport:
    start: date
    end: date
    collection: list[CollectionDay] = field(default_factory=list)
    editions: list[EditionQuality] = field(default_factory=list)
    same_edition_repeats: list[RepeatPair] = field(default_factory=list)
    cross_day_repeats: list[RepeatPair] = field(default_factory=list)
    dedupe_decisions: dict[str, dict[str, int]] = field(default_factory=dict)
    # Stars -> (candidates rated, candidates picked), from each edition's latest assembly.
    editor_ratings: dict[int, tuple[int, int]] = field(default_factory=dict)
    unpicked_high_stars: list[UnpickedCandidate] = field(default_factory=list)
    fact_check_sample: list[dict[str, Any]] = field(default_factory=list)

    @property
    def visible_items(self) -> int:
        return sum(
            edition.model_items
            - edition.removed_model_items
            + edition.manual_items
            - edition.hidden_items
            for edition in self.editions
        )


async def collection_days(database: AsyncSession, start: date, end: date) -> list[CollectionDay]:
    # The window of edition D opens at D-1 08:00 Taipei (clock.window).
    window_start = func.timezone(
        clock.TAIPEI.key,
        (NewsroomArticle.edition_date - 1) + literal_column(f"time '{clock.CUTOFF.isoformat()}'"),
    )
    stale = NewsroomArticle.published_at < window_start - STALE_BEFORE_WINDOW
    articles = (
        await database.execute(
            select(
                NewsroomArticle.edition_date,
                func.count(),
                func.count().filter(NewsroomArticle.relevant.is_(True)),
                func.count().filter(stale),
            )
            .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
            .where(
                NewsroomArticle.edition_date.between(start, end),
                NewsroomSource.kind != "manual",
            )
            .group_by(NewsroomArticle.edition_date)
        )
    ).all()
    sizes = (
        select(NewsroomEvent.id, NewsroomEvent.edition_date, func.count().label("size"))
        .join(NewsroomArticle, NewsroomArticle.event_id == NewsroomEvent.id)
        .where(NewsroomEvent.edition_date.between(start, end), NewsroomEvent.status == "open")
        .group_by(NewsroomEvent.id)
        .subquery()
    )
    placed = (
        select(NewsroomEditionItem.event_id)
        .join(NewsroomEdition, NewsroomEdition.id == NewsroomEditionItem.edition_id)
        .where(NewsroomEdition.status == "published", NewsroomEditionItem.removed_at.is_(None))
    )
    events = {
        row.edition_date: row
        for row in (
            await database.execute(
                select(
                    sizes.c.edition_date,
                    func.count().label("events"),
                    func.count().filter(sizes.c.size > 1).label("multi"),
                    func.count().filter(sizes.c.id.in_(placed)).label("published"),
                ).group_by(sizes.c.edition_date)
            )
        ).all()
    }
    days = []
    for edition_date, total, relevant, before_window in sorted(articles):
        event_row = events.get(edition_date)
        days.append(
            CollectionDay(
                edition_date=edition_date,
                articles=total,
                relevant=relevant,
                published_before_window=before_window,
                events=event_row.events if event_row else 0,
                multi_article_events=event_row.multi if event_row else 0,
                published_events=event_row.published if event_row else 0,
            )
        )
    return days


async def edition_quality(database: AsyncSession, start: date, end: date) -> list[EditionQuality]:
    rows = (
        await database.execute(
            select(
                NewsroomEdition.edition_date,
                NewsroomEdition.market_code,
                NewsroomEditionItem.id,
                NewsroomEditionItem.event_id,
                NewsroomEditionItem.origin,
                NewsroomEditionItem.stars,
                NewsroomEditionItem.removed_at,
                NewsroomEditionItem.hidden_at,
            )
            .join(NewsroomEditionItem, NewsroomEditionItem.edition_id == NewsroomEdition.id)
            .where(
                NewsroomEdition.edition_date.between(start, end),
                NewsroomEdition.status == "published",
            )
        )
    ).all()
    item_ids = [row.id for row in rows]
    event_ids = [row.event_id for row in rows]
    edited = set(
        (
            await database.execute(
                select(NewsroomEditLog.entity_type, NewsroomEditLog.entity_id).where(
                    (
                        (NewsroomEditLog.entity_type == "event")
                        & (NewsroomEditLog.action == "edit")
                        & NewsroomEditLog.entity_id.in_(event_ids or [uuid.UUID(int=0)])
                    )
                    | (
                        (NewsroomEditLog.entity_type == "item")
                        & (NewsroomEditLog.action == "edit_why")
                        & NewsroomEditLog.entity_id.in_(item_ids or [uuid.UUID(int=0)])
                    )
                )
            )
        ).all()
    )
    grouped: dict[tuple[date, str], list[Any]] = defaultdict(list)
    for row in rows:
        grouped[(row.edition_date, row.market_code)].append(row)
    editions = []
    for (edition_date, market), items in sorted(grouped.items()):
        model = [item for item in items if item.origin == "model"]
        editions.append(
            EditionQuality(
                edition_date=edition_date,
                market=market,
                model_items=len(model),
                removed_model_items=sum(1 for item in model if item.removed_at is not None),
                manual_items=sum(1 for item in items if item.origin == "manual"),
                hidden_items=sum(
                    1 for item in items if item.hidden_at is not None and item.removed_at is None
                ),
                edited_items=sum(
                    1
                    for item in items
                    if item.removed_at is None
                    and (("event", item.event_id) in edited or ("item", item.id) in edited)
                ),
                stars=dict(Counter(item.stars for item in model if item.stars is not None)),
                removed_by_stars=dict(
                    Counter(
                        item.stars
                        for item in model
                        if item.stars is not None and item.removed_at is not None
                    )
                ),
            )
        )
    return editions


_PAIRS = text(
    """
    WITH visible AS (
        SELECT DISTINCT ed.edition_date, ed.market_code, i.event_id, e.working_title
        FROM newsroom_edition_items i
        JOIN newsroom_editions ed ON ed.id = i.edition_id
        JOIN newsroom_events e ON e.id = i.event_id
        WHERE ed.status = 'published' AND i.removed_at IS NULL AND i.hidden_at IS NULL
          AND ed.edition_date BETWEEN :earliest AND :end
    ),
    pairs AS (
        SELECT a.edition_date, a.market_code, a.event_id, a.working_title,
               b.edition_date AS earlier_date, b.market_code AS earlier_market,
               b.event_id AS earlier_event_id, b.working_title AS earlier_title
        FROM visible a
        JOIN visible b
          ON a.event_id <> b.event_id
         AND (
              (b.edition_date = a.edition_date AND b.market_code = a.market_code
               AND b.event_id < a.event_id)
           OR (b.edition_date < a.edition_date
               AND b.edition_date >= a.edition_date - :lookback)
         )
        WHERE a.edition_date BETWEEN :start AND :end
    )
    SELECT p.*, (
        SELECT max(1 - (x.embedding <=> y.embedding))
        FROM newsroom_articles x, newsroom_articles y
        WHERE x.event_id = p.event_id AND y.event_id = p.earlier_event_id
          AND x.embedding IS NOT NULL AND y.embedding IS NOT NULL
    ) AS similarity
    FROM pairs p
    """
)


async def repeat_pairs(
    database: AsyncSession, start: date, end: date
) -> tuple[list[RepeatPair], list[RepeatPair]]:
    """Visible items that repeat another one: in the same edition, and from earlier days."""
    rows = (
        await database.execute(
            _PAIRS,
            {
                "start": start,
                "end": end,
                "earliest": start - timedelta(days=REPEAT_LOOKBACK_DAYS),
                "lookback": REPEAT_LOOKBACK_DAYS,
            },
        )
    ).all()
    same: list[RepeatPair] = []
    cross: dict[tuple[date, uuid.UUID], RepeatPair] = {}
    for row in rows:
        if row.similarity is None or row.similarity < clustering.SAME_EVENT_SIMILARITY:
            continue
        pair = RepeatPair(
            edition_date=row.edition_date,
            market=row.market_code,
            title=row.working_title,
            earlier_date=row.earlier_date,
            earlier_market=row.earlier_market,
            earlier_title=row.earlier_title,
            similarity=round(float(row.similarity), 3),
        )
        if row.earlier_date == row.edition_date:
            same.append(pair)
            continue
        # One row per repeated event and day, against its closest earlier item.
        key = (row.edition_date, row.event_id)
        if key not in cross or pair.similarity > cross[key].similarity:
            cross[key] = pair
    return sorted(same, key=_pair_order), sorted(cross.values(), key=_pair_order)


def _pair_order(pair: RepeatPair) -> tuple[date, str, float]:
    return (pair.edition_date, pair.market, -pair.similarity)


async def dedupe_decisions(
    database: AsyncSession, start: date, end: date
) -> dict[str, dict[str, int]]:
    rows = (
        await database.execute(
            select(NewsroomDedupeDecision.scope, NewsroomDedupeDecision.outcome, func.count())
            .where(NewsroomDedupeDecision.edition_date.between(start, end))
            .group_by(NewsroomDedupeDecision.scope, NewsroomDedupeDecision.outcome)
        )
    ).all()
    decisions: dict[str, dict[str, int]] = defaultdict(dict)
    for scope, outcome, count in rows:
        decisions[scope][outcome] = count
    return dict(decisions)


async def editor_ratings(
    database: AsyncSession, start: date, end: date
) -> tuple[dict[int, tuple[int, int]], list[UnpickedCandidate]]:
    """The editor's stars over every candidate of each edition's latest assembly run."""
    latest = (
        select(
            NewsroomEditorRating.edition_date,
            NewsroomEditorRating.market_code,
            func.max(NewsroomEditorRating.created_at).label("created_at"),
        )
        .where(NewsroomEditorRating.edition_date.between(start, end))
        .group_by(NewsroomEditorRating.edition_date, NewsroomEditorRating.market_code)
        .subquery()
    )
    rows = (
        await database.execute(
            select(NewsroomEditorRating)
            .join(
                latest,
                (latest.c.edition_date == NewsroomEditorRating.edition_date)
                & (latest.c.market_code == NewsroomEditorRating.market_code)
                & (latest.c.created_at == NewsroomEditorRating.created_at),
            )
            .order_by(
                NewsroomEditorRating.edition_date,
                NewsroomEditorRating.market_code,
                NewsroomEditorRating.position,
            )
        )
    ).scalars()
    rated: Counter[int] = Counter()
    picked: Counter[int] = Counter()
    unpicked: list[UnpickedCandidate] = []
    for row in rows:
        rated[row.stars] += 1
        if row.picked:
            picked[row.stars] += 1
        elif row.stars >= 4:
            unpicked.append(
                UnpickedCandidate(
                    row.edition_date, row.market_code, row.position, row.stars, row.event_title
                )
            )
    return {stars: (rated[stars], picked[stars]) for stars in sorted(rated)}, unpicked


async def fact_check_sample(
    database: AsyncSession, start: date, end: date, size: int
) -> list[dict[str, Any]]:
    """Visible items with their published text and sources, for checking against the originals."""
    if size <= 0:
        return []
    events = (
        await database.execute(
            select(
                NewsroomEdition.edition_date,
                NewsroomEdition.market_code,
                NewsroomEvent.id,
                NewsroomEvent.headline_zh_hant,
                NewsroomEvent.summary_zh_hant,
            )
            .join(NewsroomEditionItem, NewsroomEditionItem.edition_id == NewsroomEdition.id)
            .join(NewsroomEvent, NewsroomEvent.id == NewsroomEditionItem.event_id)
            .where(
                NewsroomEdition.edition_date.between(start, end),
                NewsroomEdition.status == "published",
                NewsroomEditionItem.removed_at.is_(None),
                NewsroomEditionItem.hidden_at.is_(None),
                NewsroomEvent.headline_zh_hant.is_not(None),
            )
            .order_by(func.md5(func.cast(NewsroomEvent.id, NewsroomEvent.working_title.type)))
            .limit(size)
        )
    ).all()
    sources = (
        await database.execute(
            select(NewsroomArticle.event_id, NewsroomSource.name, NewsroomArticle.url)
            .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
            .where(NewsroomArticle.event_id.in_([row.id for row in events] or [uuid.UUID(int=0)]))
            .order_by(NewsroomSource.trust_tier.desc(), NewsroomArticle.first_seen_at)
        )
    ).all()
    by_event: dict[uuid.UUID, list[str]] = defaultdict(list)
    for event_id, name, url in sources:
        by_event[event_id].append(f"{name} {url}")
    return [
        {
            "edition_date": row.edition_date.isoformat(),
            "market": row.market_code,
            "headline": row.headline_zh_hant,
            "summary": row.summary_zh_hant,
            "sources": by_event[row.id][:5],
        }
        for row in events
    ]


async def quality_report(
    database: AsyncSession, start: date, end: date, *, sample: int = 0
) -> QualityReport:
    same, cross = await repeat_pairs(database, start, end)
    ratings, unpicked = await editor_ratings(database, start, end)
    return QualityReport(
        start=start,
        end=end,
        collection=await collection_days(database, start, end),
        editions=await edition_quality(database, start, end),
        same_edition_repeats=same,
        cross_day_repeats=cross,
        dedupe_decisions=await dedupe_decisions(database, start, end),
        editor_ratings=ratings,
        unpicked_high_stars=unpicked,
        fact_check_sample=await fact_check_sample(database, start, end, sample),
    )
