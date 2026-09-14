import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from daily_insights_api.modules.news.contracts import Locale
from daily_insights_api.modules.news.failures import NewsFailure


class NewsItemResponse(BaseModel):
    id: uuid.UUID
    rank: int
    importance: int
    topic: str
    headline: str
    summary: str
    source_name: str
    source_hostname: str
    source_url: HttpUrl
    source_published_at: datetime | None
    # Quantitative points from the English summary; shown as chips.
    numeric_facts: list[str]
    # Null for editions generated before these were persisted.
    market: str | None
    event_key: str | None


class LatestNewsResponse(BaseModel):
    market_code: str
    target_items: int
    edition_id: uuid.UUID | None
    edition_date: date | None
    revision: int | None
    generated_at: datetime | None
    status: Literal["complete", "partial", "unavailable"]
    locale: Locale
    caveat: str | None
    items: list[NewsItemResponse]


CandidateStage = Literal["discovered", "fetch_failed", "unused", "reviewed", "dropped", "published"]
CandidateDropReason = Literal[
    "off_market", "policy", "duplicate_event", "summary_failed", "reserve"
]
ItemOrigin = Literal["model", "manual"]


class NewsAdminCounts(BaseModel):
    """Candidate stage totals for one edition, plus the hidden item count."""

    discovered: int
    fetch_failed: int
    unused: int
    reviewed: int
    dropped: int
    published: int
    hidden: int


class NewsAdminEdition(BaseModel):
    id: uuid.UUID
    revision: int
    status: Literal["complete", "partial", "unavailable"]
    generated_at: datetime
    prompt_version: str
    target_items: int
    counts: NewsAdminCounts


class NewsAdminItem(BaseModel):
    id: uuid.UUID
    rank: int
    origin: ItemOrigin
    hidden: bool
    hidden_at: datetime | None
    # The zh-hant presentation headline; the source headline when none exists.
    headline: str
    source_headline: str
    source_name: str
    source_hostname: str
    source_url: str
    source_published_at: datetime | None
    topic: str
    market: str | None
    importance: int
    event_key: str | None
    candidate_id: uuid.UUID | None


class NewsAdminCandidate(BaseModel):
    id: uuid.UUID
    stage: CandidateStage
    drop_reason: CandidateDropReason | None
    headline: str
    source_name: str
    hostname: str
    url: str
    seen_at: datetime | None
    source_published_at: datetime | None
    ai_rank: int | None
    ai_topic: str | None
    ai_market: str | None
    ai_importance: int | None
    ai_event_key: str | None
    item_id: uuid.UUID | None
    publish_run_id: uuid.UUID | None
    publish_requested_at: datetime | None
    publish_error: str | None


class NewsAdminEditionEntry(BaseModel):
    market_code: str
    # Null, with empty lists, when the date has no edition for this market.
    edition: NewsAdminEdition | None
    items: list[NewsAdminItem]
    candidates: list[NewsAdminCandidate]


class NewsAdminEditionsResponse(BaseModel):
    edition_date: date
    editions: list[NewsAdminEditionEntry]


class NewsCandidatePublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edition_id: uuid.UUID
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)

    @field_validator("candidate_ids")
    @classmethod
    def unique_candidate_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("candidate_ids must be unique")
        return value


class NewsDependencyResponse(BaseModel):
    scope: str
    state: str
    failure: NewsFailure | None
    available_at: datetime | None
    newest_article_at: datetime | None
    updated_at: datetime


class NewsRecoveryResponse(BaseModel):
    dependencies: list[NewsDependencyResponse]
