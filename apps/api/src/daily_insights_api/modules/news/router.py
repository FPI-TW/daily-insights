from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.news.access import visible_news_market_codes
from daily_insights_api.modules.news.contracts import Locale
from daily_insights_api.modules.news.editions import (
    GLOBAL_SPEC,
    MARKET_NEWS_CODES,
    EditionSpec,
    edition_spec,
)
from daily_insights_api.modules.news.models import NewsEdition, NewsItem, NewsPresentation
from daily_insights_api.modules.news.schemas import LatestNewsResponse, NewsItemResponse
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/news", tags=["news"])
Member = Annotated[AuthContext, Depends(require_password_changed)]


def _localized_caveat(status: str, count: int, locale: Locale, target: int = 5) -> str | None:
    # Complete and partial editions are presented without a caveat: the story
    # count speaks for itself and the shortfall wording was judged noise.
    del count, target
    if status in {"complete", "partial"}:
        return None
    return {
        "zh-hant": "本日重大新聞尚未產生。",
        "zh-hans": "本日重大新闻尚未生成。",
        "en": "Today's major news has not been generated.",
    }[locale]


async def _latest_response(
    database: AsyncSession, response: Response, locale: Locale, spec: EditionSpec
) -> LatestNewsResponse:
    response.headers["Cache-Control"] = "no-store"
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    edition = (
        await database.scalars(
            select(NewsEdition)
            .where(
                NewsEdition.edition_date <= today,
                NewsEdition.status.in_(("complete", "partial")),
                select(NewsItem.id)
                .join(NewsPresentation, NewsPresentation.item_id == NewsItem.id)
                .where(
                    NewsItem.edition_id == NewsEdition.id,
                    NewsItem.hidden_at.is_(None),
                    NewsPresentation.locale == locale,
                )
                .exists(),
                NewsEdition.market_code == spec.market_code,
            )
            .order_by(NewsEdition.edition_date.desc(), NewsEdition.revision.desc())
            .limit(1)
        )
    ).first()
    if edition is None:
        return LatestNewsResponse(
            market_code=spec.market_code,
            target_items=spec.target_items,
            edition_id=None,
            edition_date=None,
            revision=None,
            generated_at=None,
            status="unavailable",
            locale=locale,
            caveat=_localized_caveat("unavailable", 0, locale, spec.target_items),
            items=[],
        )
    rows = await database.execute(
        select(NewsItem, NewsPresentation)
        .join(
            NewsPresentation,
            (NewsPresentation.item_id == NewsItem.id) & (NewsPresentation.locale == locale),
        )
        # Hidden items stay in the immutable edition but are not shown to
        # readers; manually published items are ordinary items.
        .where(NewsItem.edition_id == edition.id, NewsItem.hidden_at.is_(None))
        .order_by(NewsItem.rank)
    )
    items = [
        NewsItemResponse(
            id=item.id,
            rank=item.rank,
            importance=item.importance,
            topic=item.topic,
            headline=presentation.headline,
            summary=presentation.summary,
            source_name=item.source_name,
            source_hostname=item.source_hostname,
            source_url=item.source_url,
            source_published_at=item.source_published_at,
            numeric_facts=list(item.numeric_facts),
            market=item.market,
            event_key=item.event_key,
        )
        for item, presentation in rows
    ]
    return LatestNewsResponse(
        market_code=spec.market_code,
        target_items=spec.target_items,
        edition_id=edition.id,
        edition_date=edition.edition_date,
        revision=edition.revision,
        generated_at=edition.generated_at,
        status=edition.status,
        locale=locale,
        # A reader sees the newest publishable edition as the day's news even
        # when it is older than today; the fallback is not announced.
        caveat=_localized_caveat(edition.status, len(items), locale, spec.target_items),
        items=items,
    )


@router.get("/latest", response_model=LatestNewsResponse)
async def latest_news(
    context: Member,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    response: Response,
    locale: Locale = "zh-hant",
) -> LatestNewsResponse:
    del context
    return await _latest_response(database, response, locale, GLOBAL_SPEC)


@router.get("/{market_code}/latest", response_model=LatestNewsResponse)
async def latest_market_news(
    market_code: str,
    context: Member,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    response: Response,
    locale: Locale = "zh-hant",
) -> LatestNewsResponse:
    if market_code not in MARKET_NEWS_CODES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "market news not found")
    if market_code not in await visible_news_market_codes(database, context):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "market news not found")
    return await _latest_response(database, response, locale, edition_spec(market_code))
