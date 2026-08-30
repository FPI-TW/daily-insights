import uuid
from datetime import date, datetime

from pydantic import BaseModel

from daily_insights_api.modules.reports.contracts import (
    Locale,
    PresentationContract,
    PublicationContent,
    ReportStatus,
)


class ReportSummaryResponse(BaseModel):
    publication_id: uuid.UUID
    report_key: str
    market_code: str
    edition_date: date
    revision: int
    source_as_of: date | None
    published_at: datetime
    stale: bool
    stale_reason: str | None
    status: ReportStatus
    title: str
    summary: str | None
    locale: Locale


class ReportDetailResponse(ReportSummaryResponse):
    manifest_version: str
    manifest_hash: str
    content: PublicationContent
    presentation: PresentationContract
