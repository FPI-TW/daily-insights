"""Workstream ⑤: reader endpoints under /api/newsroom (spec §6.5).

Owner: reader worktree. Mounted by web/app.py.

A reader gets the newest published edition of a market that has at least one
visible item in their locale (spec §4.5), never one dated after today in
Taipei; an older edition is returned with ``is_today = false`` so the page can
say which day it shows (D16).
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import ColumnElement, and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.data_sources.api import TRACKED_INDICES
from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.markets.api import visible_market_codes
from daily_insights_api.modules.newsroom.clock import taipei_today
from daily_insights_api.modules.newsroom.contracts import MarketCode
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEvent,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.translation import current_zh_hant_digest
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/newsroom", tags=["newsroom"])
Member = Annotated[AuthContext, Depends(require_password_changed)]
Locale = Literal["zh-hant", "zh-hans", "en"]

GLOBAL_MARKET = "global"
# Market editions follow the organization's market policy; the global edition
# is visible to every member (carried over from the legacy news access rules).
POLICY_GATED_MARKETS: tuple[str, ...] = ("tw_equity", "us_equity")
_INTERNAL_PREVIEW_ROLES = frozenset({SystemRole.ADMIN, SystemRole.ASSET_MANAGER})
# Related symbols link to the market page that charts them; the tracked indices
# are the only symbols with a dashboard of their own.
_SYMBOL_DASHBOARDS: dict[str, str] = {symbol: market for symbol, market in TRACKED_INDICES.items()}
# Editions are checked newest first in pages; a page past the first is only read
# when English translations of the newer editions are stale.
_EDITION_PAGE_SIZE = 7


class NewsroomSourceLink(BaseModel):
    name: str
    url: str
    published_at: datetime | None


class NewsroomRelatedSymbol(BaseModel):
    symbol: str
    kind: str
    label: str
    # The site's dashboard for this symbol when the viewer may open it; the
    # symbol is shown as plain text otherwise.
    market_code: str | None


class NewsroomItemResponse(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    rank: int
    stars: int | None
    headline: str
    summary: str
    why: str
    related_symbols: list[NewsroomRelatedSymbol]
    sources: list[NewsroomSourceLink]


class NewsroomEditionResponse(BaseModel):
    market_code: MarketCode
    locale: Locale
    # All null (and ``items`` empty) when no edition has a visible item yet.
    edition_id: uuid.UUID | None
    edition_date: date | None
    is_today: bool
    published_at: datetime | None
    items: list[NewsroomItemResponse]


def _live_items_filter() -> ColumnElement[bool]:
    """Items of a published edition a reader may see; mirrors the item set of
    ``translation.visible_items_query`` that the English digest covers."""
    return and_(
        NewsroomEdition.status == "published",
        NewsroomEditionItem.removed_at.is_(None),
        NewsroomEditionItem.hidden_at.is_(None),
        NewsroomEditionItem.abandoned_at.is_(None),
        NewsroomEditionItem.why_status == "ready",
    )


def _visible_items_filter(locale: Locale) -> ColumnElement[bool]:
    """Spec §4.5 visibility, minus the English digest check done in Python.

    Expects ``NewsroomEditionItem`` joined to its edition and event. The
    localized text must also be present: an item cannot be shown without it.
    """
    headline, summary, why = _localized_columns(locale)
    conditions = [
        _live_items_filter(),
        NewsroomEvent.analysis_status == "ready",
        headline.is_not(None),
        summary.is_not(None),
        why.is_not(None),
    ]
    if locale == "en":
        conditions += [
            NewsroomEvent.en_status == "ready",
            NewsroomEvent.en_source_digest.is_not(None),
            NewsroomEditionItem.why_en_status == "ready",
        ]
    return and_(*conditions)


def _localized_columns(locale: Locale) -> tuple[Any, Any, Any]:
    if locale == "zh-hans":
        return (
            NewsroomEvent.headline_zh_hans,
            NewsroomEvent.summary_zh_hans,
            NewsroomEditionItem.why_zh_hans,
        )
    if locale == "en":
        return NewsroomEvent.headline_en, NewsroomEvent.summary_en, NewsroomEditionItem.why_en
    return (
        NewsroomEvent.headline_zh_hant,
        NewsroomEvent.summary_zh_hant,
        NewsroomEditionItem.why_zh_hant,
    )


async def _visible_rows(
    database: AsyncSession, edition_id: uuid.UUID, locale: Locale
) -> list[tuple[NewsroomEditionItem, NewsroomEvent]]:
    rows = [
        (item, event)
        for item, event in await database.execute(
            select(NewsroomEditionItem, NewsroomEvent)
            .join(NewsroomEdition, NewsroomEdition.id == NewsroomEditionItem.edition_id)
            .join(NewsroomEvent, NewsroomEvent.id == NewsroomEditionItem.event_id)
            .where(NewsroomEditionItem.edition_id == edition_id, _visible_items_filter(locale))
            .order_by(NewsroomEditionItem.rank)
        )
    ]
    if locale != "en" or not rows:
        return rows
    # English is stale once the zh-hant it was translated from has changed.
    digests = {
        event.id: await current_zh_hant_digest(database, event.id)
        for event in {event.id: event for _, event in rows}.values()
    }
    return [(item, event) for item, event in rows if event.en_source_digest == digests[event.id]]


def _is_web_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


async def _sources(
    database: AsyncSession, event_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[NewsroomSourceLink]]:
    sources: dict[uuid.UUID, list[NewsroomSourceLink]] = defaultdict(list)
    rows = await database.execute(
        select(NewsroomArticle, NewsroomSource)
        .join(NewsroomSource, NewsroomSource.id == NewsroomArticle.source_id)
        .where(NewsroomArticle.event_id.in_(event_ids))
        .order_by(
            NewsroomSource.trust_tier.desc(),
            NewsroomArticle.published_at.desc().nulls_last(),
            NewsroomArticle.first_seen_at,
        )
    )
    for article, source in rows:
        if article.event_id is None or not _is_web_url(article.url):
            continue
        name = source.name
        if source.kind == "manual":
            # The manual source stands for whichever site an admin pasted; its
            # own name means nothing to a reader.
            name = urlsplit(article.url).hostname or name
        sources[article.event_id].append(
            NewsroomSourceLink(name=name, url=article.url, published_at=article.published_at)
        )
    return sources


def _related_symbols(
    raw: Sequence[Any], locale: Locale, linkable_markets: frozenset[str]
) -> list[NewsroomRelatedSymbol]:
    symbols: list[NewsroomRelatedSymbol] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        symbol, kind, label = entry.get("symbol"), entry.get("kind"), entry.get("label")
        if not (isinstance(symbol, str) and isinstance(kind, str) and isinstance(label, str)):
            continue
        # Labels come from the zh-hant analysis and are not translated; an
        # English page shows the ticker rather than Chinese text.
        if locale == "en" and not label.isascii():
            label = symbol
        market = _SYMBOL_DASHBOARDS.get(symbol)
        symbols.append(
            NewsroomRelatedSymbol(
                symbol=symbol,
                kind=kind,
                label=label,
                market_code=market if market in linkable_markets else None,
            )
        )
    return symbols


async def latest_edition(
    database: AsyncSession,
    market_code: MarketCode,
    locale: Locale,
    *,
    today: date,
    linkable_markets: frozenset[str] = frozenset(),
) -> NewsroomEditionResponse:
    """The newest published edition on or before ``today`` with a visible item."""
    has_visible_item = (
        select(NewsroomEditionItem.id)
        .join(NewsroomEvent, NewsroomEvent.id == NewsroomEditionItem.event_id)
        .where(NewsroomEditionItem.edition_id == NewsroomEdition.id, _visible_items_filter(locale))
        .exists()
    )
    offset = 0
    while True:
        editions = (
            await database.scalars(
                select(NewsroomEdition)
                .where(
                    NewsroomEdition.market_code == market_code,
                    NewsroomEdition.status == "published",
                    NewsroomEdition.edition_date <= today,
                    has_visible_item,
                )
                .order_by(NewsroomEdition.edition_date.desc())
                .offset(offset)
                .limit(_EDITION_PAGE_SIZE)
            )
        ).all()
        for edition in editions:
            rows = await _visible_rows(database, edition.id, locale)
            if rows:
                return await _edition_response(
                    database, edition, rows, market_code, locale, today, linkable_markets
                )
        if len(editions) < _EDITION_PAGE_SIZE:
            return NewsroomEditionResponse(
                market_code=market_code,
                locale=locale,
                edition_id=None,
                edition_date=None,
                is_today=False,
                published_at=None,
                items=[],
            )
        offset += _EDITION_PAGE_SIZE


async def _edition_response(
    database: AsyncSession,
    edition: NewsroomEdition,
    rows: Sequence[tuple[NewsroomEditionItem, NewsroomEvent]],
    market_code: MarketCode,
    locale: Locale,
    today: date,
    linkable_markets: frozenset[str],
) -> NewsroomEditionResponse:
    sources = await _sources(database, [event.id for _, event in rows])
    items = []
    for item, event in rows:
        headline, summary, why = _localized_text(item, event, locale)
        items.append(
            NewsroomItemResponse(
                id=item.id,
                event_id=event.id,
                rank=item.rank,
                stars=item.stars,
                headline=headline,
                summary=summary,
                why=why,
                related_symbols=_related_symbols(event.related_symbols, locale, linkable_markets),
                sources=sources[event.id],
            )
        )
    return NewsroomEditionResponse(
        market_code=market_code,
        locale=locale,
        edition_id=edition.id,
        edition_date=edition.edition_date,
        is_today=edition.edition_date == today,
        published_at=edition.published_at,
        items=items,
    )


def _localized_text(
    item: NewsroomEditionItem, event: NewsroomEvent, locale: Locale
) -> tuple[str, str, str]:
    if locale == "zh-hans":
        texts = (event.headline_zh_hans, event.summary_zh_hans, item.why_zh_hans)
    elif locale == "en":
        texts = (event.headline_en, event.summary_en, item.why_en)
    else:
        texts = (event.headline_zh_hant, event.summary_zh_hant, item.why_zh_hant)
    headline, summary, why = texts
    # The visibility filter already required every localized field.
    assert headline is not None and summary is not None and why is not None
    return headline, summary, why


async def readable_market_codes(database: AsyncSession, context: AuthContext) -> frozenset[str]:
    """Markets whose editions and dashboards the viewer may open.

    Internal roles preview every market. Members need an organization, whose
    market policy decides the rest; the global edition is never gated.
    """
    if context.user.system_role in _INTERNAL_PREVIEW_ROLES:
        return frozenset(POLICY_GATED_MARKETS) | frozenset(_SYMBOL_DASHBOARDS.values())
    if context.organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    return frozenset(await visible_market_codes(database, context.organization_id))


@router.get("/editions/latest", response_model=NewsroomEditionResponse)
async def latest_newsroom_edition(
    market: MarketCode,
    context: Member,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    response: Response,
    locale: Locale = "zh-hant",
) -> NewsroomEditionResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        readable = await readable_market_codes(database, context)
    except HTTPException:
        # Without an organization only the global edition is readable, and
        # none of its related symbols links anywhere.
        if market != GLOBAL_MARKET:
            raise
        readable = frozenset()
    if market != GLOBAL_MARKET and market not in readable:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "market news not found")
    return await latest_edition(
        database, market, locale, today=taipei_today(), linkable_markets=readable
    )
