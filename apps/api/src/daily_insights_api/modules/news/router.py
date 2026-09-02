from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.markets.api import visible_market_codes
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
_INTERNAL_PREVIEW_ROLES = frozenset({SystemRole.ADMIN, SystemRole.ASSET_MANAGER})


def _localized_caveat(status: str, count: int, locale: Locale, target: int = 5) -> str | None:
    if status == "complete" and count >= target:
        return None
    if status in {"complete", "partial"}:
        return {
            "zh-hant": f"本日完成 {count}/{target} 則新聞，其餘資料暫缺。",  # noqa: RUF001
            "zh-hans": f"本日完成 {count}/{target} 则新闻，其余资料暂缺。",  # noqa: RUF001
            "en": (
                f"Today's edition contains {count}/{target} stories; "
                "the remaining coverage is temporarily unavailable."
            ),
        }[locale]
    return {
        "zh-hant": "本日重大新聞尚未產生。",
        "zh-hans": "本日重大新闻尚未生成。",
        "en": "Today's major news has not been generated.",
    }[locale]


async def visible_news_market_codes(database: AsyncSession, context: AuthContext) -> frozenset[str]:
    """Market news editions the viewer may read; the global digest is always visible."""
    if context.user.system_role in _INTERNAL_PREVIEW_ROLES:
        return frozenset(MARKET_NEWS_CODES)
    if context.organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    visible = await visible_market_codes(database, context.organization_id)
    return frozenset(code for code in MARKET_NEWS_CODES if code in visible)


async def _latest_response(
    database: AsyncSession, response: Response, locale: Locale, spec: EditionSpec
) -> LatestNewsResponse:
    response.headers["Cache-Control"] = "no-store"
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    edition = (
        await database.scalars(
            select(NewsEdition)
            .where(
                NewsEdition.edition_date == today,
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
        .where(NewsItem.edition_id == edition.id)
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
            source_url=item.source_url,
            source_published_at=item.source_published_at,
        )
        for item, presentation in rows
    ]
    return LatestNewsResponse(
        market_code=spec.market_code,
        target_items=spec.target_items,
        edition_date=edition.edition_date,
        revision=edition.revision,
        generated_at=edition.generated_at,
        status=edition.status,
        locale=locale,
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
