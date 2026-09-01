import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, HttpUrl

from daily_insights_api.modules.news.contracts import Locale


class NewsItemResponse(BaseModel):
    id: uuid.UUID
    rank: int
    importance: int
    topic: str
    headline: str
    summary: str
    source_name: str
    source_url: HttpUrl
    source_published_at: datetime | None


class LatestNewsResponse(BaseModel):
    edition_date: date | None
    revision: int | None
    generated_at: datetime | None
    status: Literal["complete", "partial", "unavailable"]
    locale: Locale
    caveat: str | None
    items: list[NewsItemResponse]
