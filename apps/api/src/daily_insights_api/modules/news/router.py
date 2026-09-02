from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.news.contracts import Locale
from daily_insights_api.modules.news.models import NewsEdition, NewsItem, NewsPresentation
from daily_insights_api.modules.news.schemas import LatestNewsResponse, NewsItemResponse
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/news", tags=["news"])
Member = Annotated[AuthContext, Depends(require_password_changed)]


def _localized_caveat(status: str, count: int, locale: Locale) -> str | None:
    # Complete and partial editions are presented without a caveat: the story
    # count speaks for itself and the shortfall wording was judged noise.
    del count
    if status in {"complete", "partial"}:
        return None
    return {
        "zh-hant": "本日重大新聞尚未產生。",
        "zh-hans": "本日重大新闻尚未生成。",
        "en": "Today's major news has not been generated.",
    }[locale]


@router.get("/latest", response_model=LatestNewsResponse)
async def latest_news(
    context: Member,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    response: Response,
    locale: Locale = "zh-hant",
) -> LatestNewsResponse:
    del context
    response.headers["Cache-Control"] = "no-store"
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    edition = (
        await database.scalars(
            select(NewsEdition)
            .where(NewsEdition.edition_date == today)
            .order_by(NewsEdition.edition_date.desc(), NewsEdition.revision.desc())
            .limit(1)
        )
    ).first()
    if edition is None:
        return LatestNewsResponse(
            edition_id=None,
            edition_date=None,
            revision=None,
            generated_at=None,
            status="unavailable",
            locale=locale,
            caveat=_localized_caveat("unavailable", 0, locale),
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
        edition_id=edition.id,
        edition_date=edition.edition_date,
        revision=edition.revision,
        generated_at=edition.generated_at,
        status=edition.status,
        locale=locale,
        caveat=_localized_caveat(edition.status, len(items), locale),
        items=items,
    )
