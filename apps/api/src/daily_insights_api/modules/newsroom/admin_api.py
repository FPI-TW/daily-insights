"""Workstream ④: admin endpoints under /api/admin/newsroom (spec §6.4).

Reads are direct queries. Every action that touches sources, events, analysis,
translation or publication goes through the owning workstream's service
function (``sources_service``, ``events_service``, ``publishing``), which also
writes the edit log. Only the plain field updates — removing or restoring a
draft item, hiding a published one, and ordering — are written here, each with
its own ``editlog.record_edit`` entry (spec §4.6, D19).
"""

import re
import uuid
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)
from sqlalchemy import Float, Select, and_, case, cast, distinct, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.identity.api import AuthContext, require_csrf_roles, require_roles
from daily_insights_api.modules.newsroom import (
    clock,
    editlog,
    events_service,
    publishing,
    sources_service,
    translation,
)
from daily_insights_api.modules.newsroom.contracts import MarketCode, RelatedSymbol
from daily_insights_api.modules.newsroom.models import (
    BODY_MAX_CHARS,
    MARKET_CODES,
    NewsroomArticle,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEvent,
    NewsroomSource,
)
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/admin/newsroom", tags=["newsroom management"])
AdminRead = Annotated[AuthContext, Depends(require_roles(SystemRole.ADMIN))]
AdminWrite = Annotated[AuthContext, Depends(require_csrf_roles(SystemRole.ADMIN))]
Database = Annotated[AsyncSession, Depends(get_database_session)]

# Candidate list length per market; the editor pass itself only sees 30.
CANDIDATE_LIMIT = 50
# Article links shown on a review card; the event detail lists all of them.
CARD_ARTICLE_LIMIT = 8
BODY_PREVIEW_CHARS = 4_000
# Spec §6.1: a failing source is "unhealthy" once its last success is this old.
UNHEALTHY_AFTER = timedelta(hours=6)
# Spec §6.3 step 2: each extra distinct source adds 5 points, at most 20.
SOURCE_BONUS = 5
SOURCE_BONUS_CAP = 20
# Ordering moves ranks out of the way first so a future unique (edition, rank)
# constraint can never trip mid-transaction.
RANK_SHIFT = 100_000

SourceKind = Literal["rss", "rdf", "atom", "rss_full", "news_sitemap", "json_list", "guardian_api"]
SourceHealth = Literal["disabled", "pending", "healthy", "degraded", "unhealthy"]
# Mirrors of the CHECK constraints in models.py, so the client gets enums.
BodyStatus = Literal["pending", "ok", "unavailable", "rejected", "purged"]
BodySource = Literal["feed", "fetch", "manual"]
QueueStatus = Literal["idle", "pending", "done", "failed"]
AnalysisStatus = Literal["idle", "pending", "ready", "failed", "needs_body"]
TranslationStatus = Literal["idle", "pending", "ready", "failed"]
WhyStatus = Literal["pending", "ready", "failed"]
EventStatus = Literal["open", "merged"]
EventOrigin = Literal["triage", "split", "manual", "legacy"]
EditionStatus = Literal["draft", "published"]
SelectionMode = Literal["pending", "editor", "fallback", "legacy"]
ItemOrigin = Literal["model", "manual", "legacy"]
Headline = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
SOURCE_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
HTTP_URL = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


# --- Response schemas -------------------------------------------------------


class NewsroomAdminSource(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    kind: SourceKind | Literal["manual"]
    url: str | None
    hostname: str
    link_pattern: str | None
    markets: list[MarketCode]
    language_filter: list[str] | None
    full_text_in_feed: bool
    trust_tier: int
    weight: float
    poll_interval_minutes: int
    enabled: bool
    last_polled_at: datetime | None
    last_success_at: datetime | None
    next_poll_at: datetime | None
    consecutive_failures: int
    last_error_code: str | None
    last_error_at: datetime | None
    # Articles first seen in the last 7 days.
    articles_7d: int
    health: SourceHealth


class NewsroomAdminSourceList(BaseModel):
    sources: list[NewsroomAdminSource]


class NewsroomAdminArticleLink(BaseModel):
    id: uuid.UUID
    source_name: str
    hostname: str
    title: str
    url: str
    published_at: datetime | None
    body_status: BodyStatus


class NewsroomAdminEvent(BaseModel):
    """An event with its shared analysis and the facts an editor triages by."""

    id: uuid.UUID
    edition_date: date
    working_title: str
    status: EventStatus
    merged_into_id: uuid.UUID | None
    created_by: EventOrigin
    headline_zh_hant: str | None
    summary_zh_hant: str | None
    headline_zh_hans: str | None
    summary_zh_hans: str | None
    headline_en: str | None
    summary_en: str | None
    related_symbols: list[RelatedSymbol]
    analysis_status: AnalysisStatus
    analysis_error_code: str | None
    analyzed_at: datetime | None
    en_status: TranslationStatus
    en_error_code: str | None
    edited_at: datetime | None
    article_count: int
    source_count: int
    # Articles with a usable body; zero means "missing full text" (D18).
    body_ok_count: int
    articles: list[NewsroomAdminArticleLink]


class NewsroomAdminEdition(BaseModel):
    id: uuid.UUID
    edition_date: date
    market_code: MarketCode
    status: EditionStatus
    selection_mode: SelectionMode
    auto_publish_at: datetime
    late_fill_deadline: datetime
    assembled_at: datetime | None
    published_at: datetime | None
    published_by_user_id: uuid.UUID | None
    ignored_pending_triage: int
    late_fill_closed_at: datetime | None


class NewsroomAdminItem(BaseModel):
    id: uuid.UUID
    edition_id: uuid.UUID
    rank: int
    stars: int | None
    editor_score: float | None
    origin: ItemOrigin
    why_zh_hant: str | None
    why_zh_hans: str | None
    why_en: str | None
    why_status: WhyStatus
    why_error_code: str | None
    why_en_status: TranslationStatus
    removed_at: datetime | None
    hidden_at: datetime | None
    abandoned_at: datetime | None
    event: NewsroomAdminEvent


class NewsroomAdminCandidate(BaseModel):
    # This market's event score (spec §6.3 step 2) computed now.
    score: float
    event: NewsroomAdminEvent


class NewsroomAdminEditionDetail(BaseModel):
    edition_date: date
    market_code: MarketCode
    # Null until 08:00 assembly created the edition.
    edition: NewsroomAdminEdition | None
    items: list[NewsroomAdminItem]
    candidates: list[NewsroomAdminCandidate]


class NewsroomAdminItemCounts(BaseModel):
    active: int
    removed: int
    hidden: int
    abandoned: int
    ready: int
    analysis_failed: int
    needs_body: int


class NewsroomAdminMarketSummary(BaseModel):
    market_code: MarketCode
    edition: NewsroomAdminEdition | None
    counts: NewsroomAdminItemCounts


class NewsroomAdminEditionDay(BaseModel):
    edition_date: date
    is_today: bool
    # Articles of this edition window that triage has not finished.
    untriaged_articles: int
    triage_failed_articles: int
    markets: list[NewsroomAdminMarketSummary]


class NewsroomAdminArticle(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    source_key: str
    source_name: str
    hostname: str
    title: str
    url: str
    feed_summary: str | None
    published_at: datetime | None
    first_seen_at: datetime
    language: str | None
    body_status: BodyStatus
    body_source: BodySource | None
    body_quality_reason: str | None
    body_fetched_at: datetime | None
    body_length: int
    # First characters of the internal body, for checking the analysis
    # against the original. Admin-only (D4).
    body_preview: str | None
    fetch_status: QueueStatus
    fetch_error_code: str | None
    embed_status: QueueStatus
    triage_status: QueueStatus
    triage_error_code: str | None
    relevant: bool | None
    topic: str | None
    market_scores: dict[str, int]


class NewsroomAdminPlacement(BaseModel):
    item_id: uuid.UUID
    edition_id: uuid.UUID
    market_code: MarketCode
    edition_status: EditionStatus
    removed: bool
    hidden: bool


class NewsroomAdminEventDetail(BaseModel):
    event: NewsroomAdminEvent
    articles: list[NewsroomAdminArticle]
    placements: list[NewsroomAdminPlacement]


class NewsroomAdminCreatedItem(BaseModel):
    item_id: uuid.UUID


class NewsroomAdminCreatedEvent(BaseModel):
    event_id: uuid.UUID


class NewsroomAdminCreatedArticle(BaseModel):
    article_id: uuid.UUID


# --- Request schemas --------------------------------------------------------


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _unique(values: Sequence[Any], name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must be unique")


def _check_link_pattern(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        re.compile(value)
    except re.error as error:
        raise ValueError(f"link_pattern is not a valid regular expression: {error}") from None
    return value


def _check_url(value: str | None) -> str | None:
    if value is not None and not HTTP_URL.match(value):
        raise ValueError("url must be an http(s) URL")
    return value


def _check_hostname(value: str | None) -> str | None:
    if value is not None and not HOSTNAME.match(value):
        raise ValueError("hostname must be a lowercase host name")
    return value


def _check_languages(value: list[str] | None) -> list[str] | None:
    if value is not None:
        _unique(value, "language_filter")
    return value


SourceKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
SourceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Hostname = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=1, max_length=255)
]
LanguageCode = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=2, max_length=10)
]
Url = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]
LinkPattern = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class NewsroomAdminSourceCreate(_Request):
    key: SourceKey
    name: SourceName
    kind: SourceKind
    url: Url | None = None
    hostname: Hostname
    markets: list[MarketCode] = Field(default_factory=list, max_length=3)
    trust_tier: int = Field(default=2, ge=1, le=3)
    weight: float = Field(default=1.0, ge=0.5, le=2.0)
    poll_interval_minutes: int = Field(default=30, ge=5, le=1440)
    enabled: bool = True
    link_pattern: LinkPattern | None = None
    language_filter: list[LanguageCode] | None = Field(default=None, max_length=10)
    full_text_in_feed: bool = False

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not SOURCE_KEY.match(value):
            raise ValueError("key must be a lowercase slug")
        return value

    @field_validator("markets")
    @classmethod
    def _markets(cls, value: list[MarketCode]) -> list[MarketCode]:
        _unique(value, "markets")
        return value

    _url = field_validator("url")(_check_url)
    _hostname = field_validator("hostname")(_check_hostname)
    _link_pattern = field_validator("link_pattern")(_check_link_pattern)
    _languages = field_validator("language_filter")(_check_languages)


class NewsroomAdminSourceUpdate(_Request):
    """Partial update; omitted fields stay unchanged, ``null`` clears an optional one."""

    key: SourceKey | None = None
    name: SourceName | None = None
    kind: SourceKind | None = None
    url: Url | None = None
    hostname: Hostname | None = None
    markets: list[MarketCode] | None = Field(default=None, max_length=3)
    trust_tier: int | None = Field(default=None, ge=1, le=3)
    weight: float | None = Field(default=None, ge=0.5, le=2.0)
    poll_interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    enabled: bool | None = None
    link_pattern: LinkPattern | None = None
    language_filter: list[LanguageCode] | None = Field(default=None, max_length=10)
    full_text_in_feed: bool | None = None

    @field_validator("key")
    @classmethod
    def _key(cls, value: str | None) -> str | None:
        if value is not None and not SOURCE_KEY.match(value):
            raise ValueError("key must be a lowercase slug")
        return value

    @field_validator("markets")
    @classmethod
    def _markets(cls, value: list[MarketCode] | None) -> list[MarketCode] | None:
        if value is not None:
            _unique(value, "markets")
        return value

    _url = field_validator("url")(_check_url)
    _hostname = field_validator("hostname")(_check_hostname)
    _link_pattern = field_validator("link_pattern")(_check_link_pattern)
    _languages = field_validator("language_filter")(_check_languages)


# Fields that cannot be cleared: the column is NOT NULL.
REQUIRED_SOURCE_FIELDS = frozenset(
    {
        "key",
        "name",
        "kind",
        "hostname",
        "markets",
        "trust_tier",
        "weight",
        "poll_interval_minutes",
        "enabled",
        "full_text_in_feed",
    }
)


class NewsroomAdminPublishDay(_Request):
    edition_date: date


class NewsroomAdminAddItem(_Request):
    event_id: uuid.UUID


class NewsroomAdminOrder(_Request):
    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)

    @field_validator("item_ids")
    @classmethod
    def _distinct(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        _unique(value, "item_ids")
        return value


class NewsroomAdminEventEdit(_Request):
    headline: Headline | None = None
    summary: LongText | None = None
    related_symbols: list[RelatedSymbol] | None = Field(default=None, max_length=8)


class NewsroomAdminWhyEdit(_Request):
    why: LongText


class NewsroomAdminMerge(_Request):
    target_id: uuid.UUID
    source_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)

    @field_validator("source_ids")
    @classmethod
    def _distinct(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        _unique(value, "source_ids")
        return value


class NewsroomAdminSplit(_Request):
    article_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)

    @field_validator("article_ids")
    @classmethod
    def _distinct(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        _unique(value, "article_ids")
        return value


class NewsroomAdminManualBody(_Request):
    body: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=BODY_MAX_CHARS)
    ]


class NewsroomAdminManualUrl(_Request):
    url: Url
    edition_date: date

    _url = field_validator("url")(_check_url)


# --- Helpers ----------------------------------------------------------------


# ``events_service.EventServiceError.code`` → status: a missing row is 404,
# a state that changed under the editor is 409, a malformed request is 422.
EVENT_ERROR_STATUS = {
    "event_not_found": status.HTTP_404_NOT_FOUND,
    "event_not_open": status.HTTP_409_CONFLICT,
    "edition_date_mismatch": status.HTTP_409_CONFLICT,
    "invalid_merge_sources": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "empty_split": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "article_not_in_event": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "split_takes_every_article": status.HTTP_422_UNPROCESSABLE_CONTENT,
}


@contextmanager
def _service_errors(
    *, value_error_status: int = status.HTTP_422_UNPROCESSABLE_CONTENT
) -> Iterator[None]:
    """Map the owning workstream's exceptions onto HTTP statuses.

    ``EventServiceError`` by its code (``EVENT_ERROR_STATUS``), then
    ``LookupError`` → 404 and ``ValueError`` → ``value_error_status`` (422 for
    rejected input; ``publishing`` raises it for a state that does not allow
    the action, which is a 409).
    """
    try:
        yield
    except events_service.EventServiceError as error:
        raise HTTPException(
            EVENT_ERROR_STATUS.get(error.code, status.HTTP_422_UNPROCESSABLE_CONTENT), error.code
        ) from error
    except LookupError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error) or "not found") from error
    except ValueError as error:
        raise HTTPException(value_error_status, str(error) or "invalid request") from error


def _publishing_errors() -> AbstractContextManager[None]:
    return _service_errors(value_error_status=status.HTTP_409_CONFLICT)


def _now() -> datetime:
    return datetime.now(UTC)


def _source_health(source: NewsroomSource, now: datetime) -> SourceHealth:
    if not source.enabled:
        return "disabled"
    if source.consecutive_failures > 0:
        stale = source.last_success_at is None or now - source.last_success_at > UNHEALTHY_AFTER
        return "unhealthy" if stale else "degraded"
    if source.last_success_at is None:
        return "pending"
    return "healthy"


def _source_response(
    source: NewsroomSource, articles_7d: int, now: datetime
) -> NewsroomAdminSource:
    return NewsroomAdminSource(
        id=source.id,
        key=source.key,
        name=source.name,
        kind=source.kind,
        url=source.url,
        hostname=source.hostname,
        link_pattern=source.link_pattern,
        markets=[market for market in source.markets if market in MARKET_CODES],
        language_filter=source.language_filter,
        full_text_in_feed=source.full_text_in_feed,
        trust_tier=source.trust_tier,
        weight=source.weight,
        poll_interval_minutes=source.poll_interval_minutes,
        enabled=source.enabled,
        last_polled_at=source.last_polled_at,
        last_success_at=source.last_success_at,
        next_poll_at=source.next_poll_at,
        consecutive_failures=source.consecutive_failures,
        last_error_code=source.last_error_code,
        last_error_at=source.last_error_at,
        articles_7d=articles_7d,
        health=_source_health(source, now),
    )


async def _articles_since(
    database: AsyncSession, since: datetime, source_ids: Sequence[uuid.UUID] | None = None
) -> dict[uuid.UUID, int]:
    query = (
        select(NewsroomArticle.source_id, func.count())
        .where(NewsroomArticle.first_seen_at >= since)
        .group_by(NewsroomArticle.source_id)
    )
    if source_ids is not None:
        query = query.where(NewsroomArticle.source_id.in_(list(source_ids)))
    return {source_id: count for source_id, count in (await database.execute(query)).all()}


async def _source_view(database: AsyncSession, source_id: uuid.UUID) -> NewsroomAdminSource:
    database.expire_all()
    source = await database.get(NewsroomSource, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "newsroom source not found")
    now = _now()
    counts = await _articles_since(database, now - timedelta(days=7), [source_id])
    return _source_response(source, counts.get(source_id, 0), now)


async def _key_taken(
    database: AsyncSession, key: str, *, excluding: uuid.UUID | None = None
) -> bool:
    query = select(NewsroomSource.id).where(NewsroomSource.key == key)
    if excluding is not None:
        query = query.where(NewsroomSource.id != excluding)
    return (await database.scalar(query.limit(1))) is not None


def _edition_response(edition: NewsroomEdition) -> NewsroomAdminEdition:
    return NewsroomAdminEdition(
        id=edition.id,
        edition_date=edition.edition_date,
        market_code=edition.market_code,
        status=edition.status,
        selection_mode=edition.selection_mode,
        auto_publish_at=edition.auto_publish_at,
        late_fill_deadline=edition.late_fill_deadline,
        assembled_at=edition.assembled_at,
        published_at=edition.published_at,
        published_by_user_id=edition.published_by_user_id,
        ignored_pending_triage=edition.ignored_pending_triage,
        late_fill_closed_at=edition.late_fill_closed_at,
    )


def _related_symbols(raw: list[dict[str, Any]]) -> list[RelatedSymbol]:
    """Stored symbols in the analysis contract's shape; malformed entries are skipped."""
    symbols: list[RelatedSymbol] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            symbols.append(
                RelatedSymbol.model_validate(
                    {key: entry.get(key) for key in ("symbol", "kind", "label")}
                )
            )
        except ValidationError:
            continue
    return symbols


async def _event_views(
    database: AsyncSession, event_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, NewsroomAdminEvent]:
    """Events with their article counts and leading article links, by id."""
    ids = list(dict.fromkeys(event_ids))
    if not ids:
        return {}
    events = (await database.scalars(select(NewsroomEvent).where(NewsroomEvent.id.in_(ids)))).all()
    aggregates = {
        event_id: (articles, sources, bodies)
        for event_id, articles, sources, bodies in (
            await database.execute(
                select(
                    NewsroomArticle.event_id,
                    func.count(),
                    func.count(distinct(NewsroomArticle.source_id)),
                    func.count().filter(NewsroomArticle.body_status == "ok"),
                )
                .where(NewsroomArticle.event_id.in_(ids))
                .group_by(NewsroomArticle.event_id)
            )
        ).all()
    }
    links: dict[uuid.UUID, list[NewsroomAdminArticleLink]] = defaultdict(list)
    rows = (
        await database.execute(
            select(NewsroomArticle, NewsroomSource.name, NewsroomSource.hostname)
            .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
            .where(NewsroomArticle.event_id.in_(ids))
            .order_by(
                NewsroomArticle.event_id,
                NewsroomSource.trust_tier.desc(),
                NewsroomSource.weight.desc(),
                NewsroomArticle.published_at.desc().nulls_last(),
                NewsroomArticle.first_seen_at,
            )
        )
    ).all()
    for article, source_name, hostname in rows:
        assert article.event_id is not None
        bucket = links[article.event_id]
        if len(bucket) < CARD_ARTICLE_LIMIT:
            bucket.append(
                NewsroomAdminArticleLink(
                    id=article.id,
                    source_name=source_name,
                    hostname=hostname,
                    title=article.title,
                    url=article.url,
                    published_at=article.published_at,
                    body_status=article.body_status,
                )
            )
    views: dict[uuid.UUID, NewsroomAdminEvent] = {}
    for event in events:
        article_count, source_count, body_ok_count = aggregates.get(event.id, (0, 0, 0))
        views[event.id] = NewsroomAdminEvent(
            id=event.id,
            edition_date=event.edition_date,
            working_title=event.working_title,
            status=event.status,
            merged_into_id=event.merged_into_id,
            created_by=event.created_by,
            headline_zh_hant=event.headline_zh_hant,
            summary_zh_hant=event.summary_zh_hant,
            headline_zh_hans=event.headline_zh_hans,
            summary_zh_hans=event.summary_zh_hans,
            headline_en=event.headline_en,
            summary_en=event.summary_en,
            related_symbols=_related_symbols(event.related_symbols),
            analysis_status=event.analysis_status,
            analysis_error_code=event.analysis_error_code,
            analyzed_at=event.analyzed_at,
            en_status=event.en_status,
            en_error_code=event.en_error_code,
            edited_at=event.edited_at,
            article_count=article_count,
            source_count=source_count,
            body_ok_count=body_ok_count,
            articles=links.get(event.id, []),
        )
    return views


async def _edition_for(
    database: AsyncSession, edition_date: date, market_code: str
) -> NewsroomEdition | None:
    return (
        await database.scalars(
            select(NewsroomEdition).where(
                NewsroomEdition.edition_date == edition_date,
                NewsroomEdition.market_code == market_code,
            )
        )
    ).first()


def _candidate_query(
    edition_date: date, market_code: str, edition_id: uuid.UUID | None
) -> Select[tuple[uuid.UUID | None, float]]:
    """Open events of the window ranked by this market's score (spec §6.3 step 2)."""
    weighted = cast(NewsroomArticle.market_scores[market_code].astext, Float) * cast(
        NewsroomSource.weight, Float
    )
    bonus = func.least(
        SOURCE_BONUS_CAP, SOURCE_BONUS * (func.count(distinct(NewsroomArticle.source_id)) - 1)
    )
    score = (func.coalesce(func.max(weighted), 0.0) + bonus).label("score")
    conditions = [
        NewsroomEvent.edition_date == edition_date,
        NewsroomEvent.status == "open",
        NewsroomArticle.relevant.is_(True),
    ]
    if edition_id is not None:
        conditions.append(
            NewsroomEvent.id.not_in(
                select(NewsroomEditionItem.event_id).where(
                    NewsroomEditionItem.edition_id == edition_id
                )
            )
        )
    return (
        select(NewsroomArticle.event_id, score)
        .join(NewsroomEvent, NewsroomEvent.id == NewsroomArticle.event_id)
        .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
        .where(and_(*conditions))
        .group_by(NewsroomArticle.event_id)
        .order_by(score.desc(), NewsroomArticle.event_id)
        .limit(CANDIDATE_LIMIT)
    )


def _item_response(item: NewsroomEditionItem, event: NewsroomAdminEvent) -> NewsroomAdminItem:
    return NewsroomAdminItem(
        id=item.id,
        edition_id=item.edition_id,
        rank=item.rank,
        stars=item.stars,
        editor_score=item.editor_score,
        origin=item.origin,
        why_zh_hant=item.why_zh_hant,
        why_zh_hans=item.why_zh_hans,
        why_en=item.why_en,
        why_status=item.why_status,
        why_error_code=item.why_error_code,
        why_en_status=item.why_en_status,
        removed_at=item.removed_at,
        hidden_at=item.hidden_at,
        abandoned_at=item.abandoned_at,
        event=event,
    )


async def _locked_edition(database: AsyncSession, edition_id: uuid.UUID) -> NewsroomEdition:
    edition = (
        await database.scalars(
            select(NewsroomEdition)
            .where(NewsroomEdition.id == edition_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).first()
    if edition is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "newsroom edition not found")
    return edition


async def _item_and_edition(
    database: AsyncSession, item_id: uuid.UUID
) -> tuple[NewsroomEditionItem, NewsroomEdition]:
    item = await database.get(NewsroomEditionItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "newsroom item not found")
    edition = await _locked_edition(database, item.edition_id)
    await database.refresh(item)
    return item, edition


async def _open_event(database: AsyncSession, event_id: uuid.UUID) -> NewsroomEvent:
    event = await database.get(NewsroomEvent, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "newsroom event not found")
    if event.status != "open":
        raise HTTPException(status.HTTP_409_CONFLICT, "event was merged into another event")
    return event


# --- Sources ----------------------------------------------------------------


@router.get("/sources", response_model=NewsroomAdminSourceList)
async def list_sources(_: AdminRead, database: Database) -> NewsroomAdminSourceList:
    """Every source with its polling health and 7-day article count (D3)."""
    now = _now()
    sources = (
        await database.scalars(
            select(NewsroomSource).order_by(NewsroomSource.enabled.desc(), NewsroomSource.name)
        )
    ).all()
    counts = await _articles_since(database, now - timedelta(days=7))
    return NewsroomAdminSourceList(
        sources=[_source_response(source, counts.get(source.id, 0), now) for source in sources]
    )


@router.post(
    "/sources",
    response_model=NewsroomAdminSource,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_409_CONFLICT: {"description": "Source key already exists."}},
)
async def create_source(
    payload: NewsroomAdminSourceCreate, actor: AdminWrite, database: Database
) -> NewsroomAdminSource:
    if await _key_taken(database, payload.key):
        raise HTTPException(status.HTTP_409_CONFLICT, "source key already exists")
    data = sources_service.SourceInput(
        key=payload.key,
        name=payload.name,
        kind=payload.kind,
        url=payload.url,
        hostname=payload.hostname,
        markets=tuple(payload.markets),
        trust_tier=payload.trust_tier,
        weight=payload.weight,
        poll_interval_minutes=payload.poll_interval_minutes,
        enabled=payload.enabled,
        link_pattern=payload.link_pattern,
        language_filter=tuple(payload.language_filter)
        if payload.language_filter is not None
        else None,
        full_text_in_feed=payload.full_text_in_feed,
    )
    with _service_errors():
        source_id = await sources_service.create_source(database, data, user_id=actor.user.id)
    await database.commit()
    return await _source_view(database, source_id)


@router.patch(
    "/sources/{source_id}",
    response_model=NewsroomAdminSource,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Source not found."},
        status.HTTP_409_CONFLICT: {
            "description": "Source key already exists, or the manual source's kind changed."
        },
    },
)
async def update_source(
    source_id: uuid.UUID,
    payload: NewsroomAdminSourceUpdate,
    actor: AdminWrite,
    database: Database,
) -> NewsroomAdminSource:
    """Edit settings or enable/disable a source (D3); omitted fields are unchanged."""
    source = await database.get(NewsroomSource, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "newsroom source not found")
    changes: dict[str, object] = payload.model_dump(exclude_unset=True)
    cleared = sorted(
        name for name, value in changes.items() if value is None and name in REQUIRED_SOURCE_FIELDS
    )
    if cleared:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"cannot clear required fields: {cleared}"
        )
    if not changes:
        return await _source_view(database, source_id)
    if source.kind == "manual" and "kind" in changes:
        raise HTTPException(status.HTTP_409_CONFLICT, "the manual source's kind is fixed")
    key = changes.get("key")
    if isinstance(key, str) and await _key_taken(database, key, excluding=source_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "source key already exists")
    for name in ("markets", "language_filter"):
        value = changes.get(name)
        if isinstance(value, list):
            changes[name] = tuple(value)
    with _service_errors():
        await sources_service.update_source(database, source_id, changes, user_id=actor.user.id)
    await database.commit()
    return await _source_view(database, source_id)


# --- Editions ---------------------------------------------------------------


@router.get("/editions", response_model=NewsroomAdminEditionDay)
async def edition_day(
    _: AdminRead,
    database: Database,
    edition_date: Annotated[date | None, Query(alias="date")] = None,
) -> NewsroomAdminEditionDay:
    """The three market editions of one date (default: today in Taipei)."""
    today = clock.taipei_today()
    resolved = edition_date or today
    editions = {
        edition.market_code: edition
        for edition in (
            await database.scalars(
                select(NewsroomEdition).where(NewsroomEdition.edition_date == resolved)
            )
        ).all()
    }
    counts: dict[uuid.UUID, NewsroomAdminItemCounts] = {}
    if editions:
        live = and_(
            NewsroomEditionItem.removed_at.is_(None),
            NewsroomEditionItem.hidden_at.is_(None),
            NewsroomEditionItem.abandoned_at.is_(None),
        )
        rows = (
            await database.execute(
                select(
                    NewsroomEditionItem.edition_id,
                    func.count().filter(live),
                    func.count().filter(NewsroomEditionItem.removed_at.is_not(None)),
                    func.count().filter(NewsroomEditionItem.hidden_at.is_not(None)),
                    func.count().filter(NewsroomEditionItem.abandoned_at.is_not(None)),
                    func.count().filter(
                        live,
                        NewsroomEvent.analysis_status == "ready",
                        NewsroomEditionItem.why_status == "ready",
                    ),
                    func.count().filter(
                        live,
                        (NewsroomEvent.analysis_status == "failed")
                        | (NewsroomEditionItem.why_status == "failed"),
                    ),
                    func.count().filter(live, NewsroomEvent.analysis_status == "needs_body"),
                )
                .join(NewsroomEvent, NewsroomEvent.id == NewsroomEditionItem.event_id)
                .where(NewsroomEditionItem.edition_id.in_([e.id for e in editions.values()]))
                .group_by(NewsroomEditionItem.edition_id)
            )
        ).all()
        for edition_id, active, removed, hidden, abandoned, ready, failed, needs_body in rows:
            counts[edition_id] = NewsroomAdminItemCounts(
                active=active,
                removed=removed,
                hidden=hidden,
                abandoned=abandoned,
                ready=ready,
                analysis_failed=failed,
                needs_body=needs_body,
            )
    empty = NewsroomAdminItemCounts(
        active=0, removed=0, hidden=0, abandoned=0, ready=0, analysis_failed=0, needs_body=0
    )
    untriaged, triage_failed = (
        await database.execute(
            select(
                func.count().filter(NewsroomArticle.triage_status.in_(("idle", "pending"))),
                func.count().filter(NewsroomArticle.triage_status == "failed"),
            ).where(
                NewsroomArticle.edition_date == resolved,
                # Articles whose embedding failed never reach triage.
                NewsroomArticle.embed_status != "failed",
            )
        )
    ).one()
    return NewsroomAdminEditionDay(
        edition_date=resolved,
        is_today=resolved == today,
        untriaged_articles=untriaged,
        triage_failed_articles=triage_failed,
        markets=[
            NewsroomAdminMarketSummary(
                market_code=market_code,
                edition=_edition_response(editions[market_code])
                if market_code in editions
                else None,
                counts=counts.get(editions[market_code].id, empty)
                if market_code in editions
                else empty,
            )
            for market_code in MARKET_CODES
        ],
    )


@router.get("/editions/{edition_date}/{market_code}", response_model=NewsroomAdminEditionDetail)
async def edition_detail(
    edition_date: date, market_code: MarketCode, _: AdminRead, database: Database
) -> NewsroomAdminEditionDetail:
    """One market's items in rank order plus the events that could be added."""
    edition = await _edition_for(database, edition_date, market_code)
    items: list[NewsroomEditionItem] = []
    if edition is not None:
        items = list(
            (
                await database.scalars(
                    select(NewsroomEditionItem)
                    .where(NewsroomEditionItem.edition_id == edition.id)
                    .order_by(NewsroomEditionItem.rank, NewsroomEditionItem.created_at)
                )
            ).all()
        )
    candidate_rows = (
        await database.execute(
            _candidate_query(edition_date, market_code, edition.id if edition else None)
        )
    ).all()
    candidate_scores = [
        (event_id, float(score)) for event_id, score in candidate_rows if event_id is not None
    ]
    views = await _event_views(
        database,
        [item.event_id for item in items] + [event_id for event_id, _ in candidate_scores],
    )
    return NewsroomAdminEditionDetail(
        edition_date=edition_date,
        market_code=market_code,
        edition=_edition_response(edition) if edition is not None else None,
        items=[_item_response(item, views[item.event_id]) for item in items],
        candidates=[
            NewsroomAdminCandidate(score=score, event=views[event_id])
            for event_id, score in candidate_scores
            if event_id in views
        ],
    )


@router.post(
    "/editions/{edition_id}/publish",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Edition not found."},
        status.HTTP_409_CONFLICT: {"description": "Edition is already published."},
    },
)
async def publish_edition(edition_id: uuid.UUID, actor: AdminWrite, database: Database) -> None:
    """Approve one market's draft now instead of waiting for 09:00 (D1)."""
    edition = await _locked_edition(database, edition_id)
    if edition.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "edition is already published")
    with _publishing_errors():
        await publishing.publish_edition(database, edition.id, user_id=actor.user.id)
    await database.commit()


@router.post("/editions/publish", status_code=status.HTTP_204_NO_CONTENT)
async def publish_day(
    payload: NewsroomAdminPublishDay, actor: AdminWrite, database: Database
) -> None:
    """Approve every market still in draft for one date; published ones are skipped."""
    editions = (
        await database.scalars(
            select(NewsroomEdition)
            .where(
                NewsroomEdition.edition_date == payload.edition_date,
                NewsroomEdition.status == "draft",
            )
            .order_by(NewsroomEdition.market_code)
            .with_for_update()
        )
    ).all()
    with _publishing_errors():
        for edition in editions:
            await publishing.publish_edition(database, edition.id, user_id=actor.user.id)
    await database.commit()


@router.post(
    "/editions/{edition_id}/items",
    response_model=NewsroomAdminCreatedItem,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Edition or event not found."},
        status.HTTP_409_CONFLICT: {
            "description": "Event is merged, from another date, or already in the edition."
        },
    },
)
async def add_item(
    edition_id: uuid.UUID,
    payload: NewsroomAdminAddItem,
    actor: AdminWrite,
    database: Database,
) -> NewsroomAdminCreatedItem:
    """Place a candidate event in this market's edition (D19)."""
    edition = await _locked_edition(database, edition_id)
    event = await _open_event(database, payload.event_id)
    if event.edition_date != edition.edition_date:
        # D14: events never cross edition windows.
        raise HTTPException(status.HTTP_409_CONFLICT, "event belongs to another edition date")
    existing = await database.scalar(
        select(NewsroomEditionItem.id).where(
            NewsroomEditionItem.edition_id == edition.id,
            NewsroomEditionItem.event_id == event.id,
        )
    )
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "event is already in this edition")
    with _publishing_errors():
        item_id = await publishing.add_event_to_edition(
            database, edition.id, event.id, user_id=actor.user.id
        )
    await database.commit()
    return NewsroomAdminCreatedItem(item_id=item_id)


@router.put(
    "/editions/{edition_id}/order",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Edition not found."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "item_ids is not exactly the edition's items."
        },
    },
)
async def reorder_items(
    edition_id: uuid.UUID,
    payload: NewsroomAdminOrder,
    actor: AdminWrite,
    database: Database,
) -> None:
    """Renumber ranks 1..n in the given order; the list must name every item once."""
    edition = await _locked_edition(database, edition_id)
    current = list(
        (
            await database.execute(
                select(NewsroomEditionItem.id, NewsroomEditionItem.rank)
                .where(NewsroomEditionItem.edition_id == edition.id)
                .order_by(NewsroomEditionItem.rank, NewsroomEditionItem.created_at)
            )
        ).all()
    )
    if {item_id for item_id, _ in current} != set(payload.item_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "item_ids must list every item of the edition"
        )
    before = [str(item_id) for item_id, _ in current]
    after = [str(item_id) for item_id in payload.item_ids]
    ranks = {item_id: rank for item_id, rank in current}
    if before == after and all(ranks[item_id] == index + 1 for index, item_id in enumerate(ranks)):
        return
    await database.execute(
        update(NewsroomEditionItem)
        .where(NewsroomEditionItem.edition_id == edition.id)
        .values(rank=NewsroomEditionItem.rank + RANK_SHIFT)
    )
    await database.execute(
        update(NewsroomEditionItem)
        .where(NewsroomEditionItem.edition_id == edition.id)
        .values(
            rank=case(
                {item_id: index + 1 for index, item_id in enumerate(payload.item_ids)},
                value=NewsroomEditionItem.id,
            )
        )
        .execution_options(synchronize_session=False)
    )
    editlog.record_edit(
        database,
        entity_type="edition",
        entity_id=edition.id,
        action="reorder",
        user_id=actor.user.id,
        before={"order": before},
        after={"order": after},
    )
    await database.commit()


# --- Items ------------------------------------------------------------------


async def _set_item_timestamp(
    database: AsyncSession,
    item_id: uuid.UUID,
    *,
    field: Literal["removed_at", "hidden_at"],
    value: bool,
    actor: AuthContext,
) -> None:
    item, edition = await _item_and_edition(database, item_id)
    required = "draft" if field == "removed_at" else "published"
    if edition.status != required:
        message = (
            "only draft items can be removed; hide a published item instead"
            if field == "removed_at"
            else "only published items can be hidden; remove a draft item instead"
        )
        raise HTTPException(status.HTTP_409_CONFLICT, message)
    current = getattr(item, field) is not None
    if current == value:
        # Idempotent: a repeated request neither rewrites the time nor logs twice.
        return
    moment = _now() if value else None
    setattr(item, field, moment)
    if field == "hidden_at":
        item.hidden_by_user_id = actor.user.id if value else None
    noun = "removed" if field == "removed_at" else "hidden"
    editlog.record_edit(
        database,
        entity_type="item",
        entity_id=item.id,
        action=("remove" if value else "restore")
        if field == "removed_at"
        else ("hide" if value else "unhide"),
        user_id=actor.user.id,
        before={noun: current},
        after={noun: value, "edition_id": str(edition.id), "event_id": str(item.event_id)},
    )
    # Visibility changed what readers see: re-translate now rather than at
    # the next per-minute sweep.
    await translation.mark_english_stale(database, item.event_id)
    await database.commit()


@router.post(
    "/items/{item_id}/remove",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Item not found."},
        status.HTTP_409_CONFLICT: {"description": "Edition is already published."},
    },
)
async def remove_item(item_id: uuid.UUID, actor: AdminWrite, database: Database) -> None:
    await _set_item_timestamp(database, item_id, field="removed_at", value=True, actor=actor)


@router.post(
    "/items/{item_id}/restore",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Item not found."},
        status.HTTP_409_CONFLICT: {"description": "Edition is already published."},
    },
)
async def restore_item(item_id: uuid.UUID, actor: AdminWrite, database: Database) -> None:
    await _set_item_timestamp(database, item_id, field="removed_at", value=False, actor=actor)


@router.post(
    "/items/{item_id}/hide",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Item not found."},
        status.HTTP_409_CONFLICT: {"description": "Edition is still a draft."},
    },
)
async def hide_item(item_id: uuid.UUID, actor: AdminWrite, database: Database) -> None:
    await _set_item_timestamp(database, item_id, field="hidden_at", value=True, actor=actor)


@router.post(
    "/items/{item_id}/unhide",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Item not found."},
        status.HTTP_409_CONFLICT: {"description": "Edition is still a draft."},
    },
)
async def unhide_item(item_id: uuid.UUID, actor: AdminWrite, database: Database) -> None:
    await _set_item_timestamp(database, item_id, field="hidden_at", value=False, actor=actor)


@router.put(
    "/items/{item_id}/why",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Item not found."},
        status.HTTP_409_CONFLICT: {"description": 'The item\'s "why" is not ready yet.'},
    },
)
async def edit_why(
    item_id: uuid.UUID, payload: NewsroomAdminWhyEdit, actor: AdminWrite, database: Database
) -> None:
    """Rewrite this market's zh-hant "why it matters" (D7: the only editable language)."""
    item, _ = await _item_and_edition(database, item_id)
    if item.why_status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "why is not ready yet")
    with _publishing_errors():
        await publishing.apply_why_edit(database, item.id, payload.why, user_id=actor.user.id)
    await database.commit()


# --- Events -----------------------------------------------------------------


@router.get("/events/{event_id}", response_model=NewsroomAdminEventDetail)
async def event_detail(
    event_id: uuid.UUID, _: AdminRead, database: Database
) -> NewsroomAdminEventDetail:
    """Every article of an event, with a body preview for checking the original."""
    views = await _event_views(database, [event_id])
    if event_id not in views:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "newsroom event not found")
    rows = (
        await database.execute(
            select(
                NewsroomArticle,
                NewsroomSource.key,
                NewsroomSource.name,
                NewsroomSource.hostname,
                func.substr(NewsroomArticle.body, 1, BODY_PREVIEW_CHARS),
                func.char_length(func.coalesce(NewsroomArticle.body, "")),
            )
            .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
            .where(NewsroomArticle.event_id == event_id)
            .order_by(
                NewsroomSource.trust_tier.desc(),
                NewsroomArticle.published_at.desc().nulls_last(),
                NewsroomArticle.first_seen_at,
            )
        )
    ).all()
    placements = (
        await database.execute(
            select(NewsroomEditionItem, NewsroomEdition.market_code, NewsroomEdition.status)
            .join(NewsroomEdition, NewsroomEdition.id == NewsroomEditionItem.edition_id)
            .where(NewsroomEditionItem.event_id == event_id)
            .order_by(NewsroomEdition.market_code)
        )
    ).all()
    return NewsroomAdminEventDetail(
        event=views[event_id],
        articles=[
            NewsroomAdminArticle(
                id=article.id,
                source_id=article.source_id,
                source_key=source_key,
                source_name=source_name,
                hostname=hostname,
                title=article.title,
                url=article.url,
                feed_summary=article.feed_summary,
                published_at=article.published_at,
                first_seen_at=article.first_seen_at,
                language=article.language,
                body_status=article.body_status,
                body_source=article.body_source,
                body_quality_reason=article.body_quality_reason,
                body_fetched_at=article.body_fetched_at,
                body_length=body_length,
                body_preview=preview,
                fetch_status=article.fetch_status,
                fetch_error_code=article.fetch_error_code,
                embed_status=article.embed_status,
                triage_status=article.triage_status,
                triage_error_code=article.triage_error_code,
                relevant=article.relevant,
                topic=article.topic,
                market_scores=article.market_scores,
            )
            for article, source_key, source_name, hostname, preview, body_length in rows
        ],
        placements=[
            NewsroomAdminPlacement(
                item_id=item.id,
                edition_id=item.edition_id,
                market_code=market_code,
                edition_status=edition_status,
                removed=item.removed_at is not None,
                hidden=item.hidden_at is not None,
            )
            for item, market_code, edition_status in placements
        ],
    )


@router.patch(
    "/events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Event not found."},
        status.HTTP_409_CONFLICT: {"description": "Event is merged or its analysis is not ready."},
    },
)
async def edit_event(
    event_id: uuid.UUID, payload: NewsroomAdminEventEdit, actor: AdminWrite, database: Database
) -> None:
    """Rewrite the shared zh-hant headline, summary or related symbols (D7)."""
    if payload.headline is None and payload.summary is None and payload.related_symbols is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "nothing to change")
    event = await _open_event(database, event_id)
    if event.analysis_status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, "analysis is not ready yet")
    with _publishing_errors():
        await publishing.apply_event_edit(
            database,
            event.id,
            headline=payload.headline,
            summary=payload.summary,
            related_symbols=[symbol.model_dump() for symbol in payload.related_symbols]
            if payload.related_symbols is not None
            else None,
            user_id=actor.user.id,
        )
    await database.commit()


@router.post(
    "/events/{event_id}/reanalyze",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Event not found."},
        status.HTTP_409_CONFLICT: {"description": "Event is merged."},
    },
)
async def reanalyze_event(event_id: uuid.UUID, actor: AdminWrite, database: Database) -> None:
    """Queue the event's analysis and every placement's "why" again (spec §6.4)."""
    event = await _open_event(database, event_id)
    with _publishing_errors():
        await publishing.reanalyze_event(database, event.id, user_id=actor.user.id)
    await database.commit()


@router.post(
    "/events/merge",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "An event was not found."},
        status.HTTP_409_CONFLICT: {
            "description": "An event is merged already or from another edition date."
        },
    },
)
async def merge_events(payload: NewsroomAdminMerge, actor: AdminWrite, database: Database) -> None:
    """Fold ``source_ids`` into ``target_id`` (D10)."""
    if payload.target_id in payload.source_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "target cannot also be a merge source"
        )
    target = await _open_event(database, payload.target_id)
    for source_id in payload.source_ids:
        source = await _open_event(database, source_id)
        if source.edition_date != target.edition_date:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "events from different edition dates cannot merge"
            )
    with _service_errors():
        await events_service.merge_events(
            database,
            target_id=target.id,
            source_ids=payload.source_ids,
            user_id=actor.user.id,
        )
    await database.commit()


@router.post(
    "/events/{event_id}/split",
    response_model=NewsroomAdminCreatedEvent,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Event not found."},
        status.HTTP_409_CONFLICT: {"description": "Event is merged."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Articles are not all in the event, or none would remain."
        },
    },
)
async def split_event(
    event_id: uuid.UUID, payload: NewsroomAdminSplit, actor: AdminWrite, database: Database
) -> NewsroomAdminCreatedEvent:
    """Move the chosen articles into a new event (D10)."""
    event = await _open_event(database, event_id)
    article_ids = set(
        (
            await database.scalars(
                select(NewsroomArticle.id).where(NewsroomArticle.event_id == event.id)
            )
        ).all()
    )
    if not set(payload.article_ids) <= article_ids:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "every article must belong to the event"
        )
    if len(payload.article_ids) == len(article_ids):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "at least one article must stay in the event"
        )
    with _service_errors():
        new_id = await events_service.split_event(
            database, event_id=event.id, article_ids=payload.article_ids, user_id=actor.user.id
        )
    await database.commit()
    return NewsroomAdminCreatedEvent(event_id=new_id)


# --- Articles ---------------------------------------------------------------


@router.put(
    "/articles/{article_id}/body",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={status.HTTP_404_NOT_FOUND: {"description": "Article not found."}},
)
async def set_manual_body(
    article_id: uuid.UUID, payload: NewsroomAdminManualBody, actor: AdminWrite, database: Database
) -> None:
    """Paste the full text for an article the fetcher could not read (D18)."""
    if await database.get(NewsroomArticle, article_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "newsroom article not found")
    with _service_errors():
        await sources_service.set_manual_body(
            database, article_id, payload.body, user_id=actor.user.id
        )
    await database.commit()


@router.post(
    "/articles/manual",
    response_model=NewsroomAdminCreatedArticle,
    status_code=status.HTTP_201_CREATED,
)
async def submit_manual_url(
    payload: NewsroomAdminManualUrl, actor: AdminWrite, database: Database
) -> NewsroomAdminCreatedArticle:
    """Queue an off-pool URL for fetch → embedding → triage into ``edition_date``."""
    with _service_errors():
        article_id = await sources_service.submit_manual_url(
            database, payload.url, edition_date=payload.edition_date, user_id=actor.user.id
        )
    await database.commit()
    return NewsroomAdminCreatedArticle(article_id=article_id)
