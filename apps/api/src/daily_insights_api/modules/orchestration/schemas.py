import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ProviderCatalogItem(BaseModel):
    key: str
    display_name: str
    ready: bool
    functions: list[str]
    last_status: str | None = None
    last_attempt_at: datetime | None = None


class FunctionCatalogItem(BaseModel):
    key: str
    provider_key: str
    freshness_days: int
    retryable: bool
    resources: list[str]
    last_status: str | None = None
    last_attempt_at: datetime | None = None


class JobCatalogItem(BaseModel):
    key: str
    kind: str
    triggers: list[str]
    functions: list[str]
    projection_handler: str | None


class OrchestrationCatalog(BaseModel):
    taipei_date: date
    registry_version: str
    registry_digest: str
    providers: list[ProviderCatalogItem]
    functions: list[FunctionCatalogItem]
    jobs: list[JobCatalogItem]
    routine_key: str
    manual_market_jobs: list[str]
    features: dict[str, bool]


class JobRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_key: Literal[
        "global_macro_refresh",
        "us_equity_refresh",
        "tw_equity_refresh",
        "news_daily_update",
        "news_global_refresh_job",
        "news_tw_equity_refresh_job",
        "news_us_equity_refresh_job",
    ]


class FunctionAttemptResponse(BaseModel):
    id: uuid.UUID
    attempt_number: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    source_as_of: date | None
    fetched_at: datetime | None
    record_count: int | None
    payload_digest: str | None
    request_metadata: list[dict[str, Any]]
    result: dict[str, Any] | None
    error_code: str | None
    error_detail: str | None


class FunctionRunResponse(BaseModel):
    id: uuid.UUID
    function_key: str
    provider_key: str
    scope: dict[str, Any]
    status: str
    missing_scopes: list[str] | None
    attempt_count: int
    next_attempt_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    result: dict[str, Any] | None
    error: str | None
    attempts: list[FunctionAttemptResponse]
    depends_on: list[uuid.UUID]


class JobRunResponse(BaseModel):
    id: uuid.UUID
    routine_run_id: uuid.UUID | None
    job_key: str
    kind: str
    trigger: str
    edition_date: date
    deadline_at: datetime | None
    status: str
    requested_by_user_id: uuid.UUID | None
    payload: dict[str, Any] | None
    started_at: datetime | None
    completed_at: datetime | None
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime
    functions: list[FunctionRunResponse]
    depends_on: list[uuid.UUID]
    downstream_jobs: list[uuid.UUID]


class JobRunList(BaseModel):
    items: list[JobRunResponse]
    page: int
    page_size: int
    total: int
    has_more: bool


class RoutineRunResponse(BaseModel):
    id: uuid.UUID
    routine_key: str
    registry_version: str
    edition_date: date
    scheduled_for: datetime
    deadline_at: datetime
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    result: dict[str, Any] | None
    created_at: datetime
    jobs: list[JobRunResponse]


class RoutineRunList(BaseModel):
    items: list[RoutineRunResponse]
    page: int
    page_size: int
    total: int
    has_more: bool


class LegacyRunResponse(BaseModel):
    id: uuid.UUID
    operation: str
    market_code: str | None
    edition_date: date
    status: str
    requested_by_user_id: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime


class LegacyRunList(BaseModel):
    items: list[LegacyRunResponse]
    page: int
    page_size: int
    total: int
    has_more: bool
