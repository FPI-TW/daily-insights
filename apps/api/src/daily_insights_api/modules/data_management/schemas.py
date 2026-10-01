import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from daily_insights_api.modules.data_management.models import DataManagementRun
from daily_insights_api.modules.reports.api import LaunchMarketCode

ProviderRerunCode = Literal["twelve_data", "yahoo_finance", "twse"]
RunOperation = Literal[
    "morning_all",
    "morning_market",
    "index_yahoo",
    "institutional_twse",
    "macro_dashboard",
    "provider_rerun",
]
RunStatus = Literal["pending", "running", "succeeded", "partial", "failed", "cancelled"]


class _DataManagementRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MorningAllRunCreate(_DataManagementRunCreate):
    operation: Literal["morning_all"]
    market_code: None = None


class MorningMarketRunCreate(_DataManagementRunCreate):
    operation: Literal["morning_market"]
    market_code: LaunchMarketCode


class IndexYahooRunCreate(_DataManagementRunCreate):
    operation: Literal["index_yahoo"]
    market_code: None = None


class InstitutionalTwseRunCreate(_DataManagementRunCreate):
    """Fetch TWSE institutional flows: per-stock for the edition date, market
    totals back to a rolling window of trading days."""

    operation: Literal["institutional_twse"]
    market_code: None = None


class MacroDashboardRunCreate(_DataManagementRunCreate):
    operation: Literal["macro_dashboard"]
    market_code: None = None


class ProviderRerunCreate(_DataManagementRunCreate):
    operation: Literal["provider_rerun"]
    provider: ProviderRerunCode


DataManagementRunCreate = Annotated[
    MorningAllRunCreate | MacroDashboardRunCreate | ProviderRerunCreate,
    Field(discriminator="operation"),
]


class DataManagementCatalog(BaseModel):
    taipei_date: date
    morning_reports_enabled: bool
    yfinance_enabled: bool
    twse_enabled: bool
    markets: list[LaunchMarketCode]
    rerunnable_providers: list[ProviderRerunCode]
    macro_dashboard_enabled: bool


class _DataManagementRunResponse(BaseModel):
    id: uuid.UUID
    edition_date: date
    status: RunStatus
    requested_by_user_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result: dict[str, Any] | None
    error: str | None
    scheduled_for: datetime | None = None
    heartbeat_at: datetime | None = None


class MorningAllRunResponse(_DataManagementRunResponse):
    operation: Literal["morning_all"]
    market_code: None


class MorningMarketRunResponse(_DataManagementRunResponse):
    operation: Literal["morning_market"]
    market_code: LaunchMarketCode


class IndexYahooRunResponse(_DataManagementRunResponse):
    operation: Literal["index_yahoo"]
    market_code: None


class InstitutionalTwseRunResponse(_DataManagementRunResponse):
    operation: Literal["institutional_twse"]
    market_code: None


class MacroDashboardRunResponse(_DataManagementRunResponse):
    operation: Literal["macro_dashboard"]
    market_code: None


class ProviderRerunResponse(_DataManagementRunResponse):
    operation: Literal["provider_rerun"]
    provider: ProviderRerunCode
    market_code: None = None


DataManagementRunResponse = Annotated[
    MorningAllRunResponse
    | MorningMarketRunResponse
    | IndexYahooRunResponse
    | InstitutionalTwseRunResponse
    | MacroDashboardRunResponse
    | ProviderRerunResponse,
    Field(discriminator="operation"),
]


class DataManagementRunList(BaseModel):
    items: list[DataManagementRunResponse]
    page: int = Field(ge=1)
    page_size: Literal[10] = 10
    total: int = Field(ge=0)
    has_more: bool
    active_runs: list[DataManagementRunResponse]


def run_response(run: DataManagementRun) -> DataManagementRunResponse:
    values = dict(
        id=run.id,
        edition_date=run.edition_date,
        status=run.status,
        requested_by_user_id=run.requested_by_user_id,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        scheduled_for=run.scheduled_for,
        heartbeat_at=run.heartbeat_at,
        result=run.result,
        error=run.error,
    )
    if run.operation == "morning_all":
        return MorningAllRunResponse(operation="morning_all", market_code=None, **values)
    if run.operation == "morning_market":
        return MorningMarketRunResponse(
            operation="morning_market",
            market_code=cast(str, run.market_code),
            **values,
        )
    if run.operation == "institutional_twse":
        return InstitutionalTwseRunResponse(
            operation="institutional_twse", market_code=None, **values
        )
    if run.operation == "macro_dashboard":
        return MacroDashboardRunResponse(operation="macro_dashboard", market_code=None, **values)
    if run.operation == "provider_rerun":
        return ProviderRerunResponse(
            operation="provider_rerun",
            provider=cast(str, run.market_code),
            market_code=None,
            **values,
        )
    return IndexYahooRunResponse(operation="index_yahoo", market_code=None, **values)
