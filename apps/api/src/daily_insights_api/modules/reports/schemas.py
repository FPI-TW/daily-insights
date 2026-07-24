import uuid
from datetime import date, datetime

from pydantic import BaseModel

from daily_insights_api.modules.reports.contracts import (
    Locale,
    PresentationContract,
    PublicationContent,
)


class ReportPublicationResponse(BaseModel):
    publication_id: uuid.UUID
    report_key: str
    market_code: str
    edition_date: date
    revision: int
    source_as_of: date
    published_at: datetime
    stale: bool
    stale_reason: str | None
    content: PublicationContent
    presentation: PresentationContract
    locale: Locale
