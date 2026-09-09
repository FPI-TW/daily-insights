import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from daily_insights_api.modules.data_management.models import DataManagementRun
from daily_insights_api.modules.reports.api import LaunchMarketCode

NewsMarketCode = Literal["global", "tw_equity", "us_equity"]
RunOperation = Literal[
    "morning_all",
    "morning_market",
    "index_yahoo",
    "institutional_twse",
    "news_all",
    "news_market",
    "news_publish",
    "macro_dashboard",
]
RunOperationGroup = Literal["news"]
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


class NewsAllRunCreate(_DataManagementRunCreate):
    operation: Literal["news_all"]
    market_code: None = None


class NewsMarketRunCreate(_DataManagementRunCreate):
    operation: Literal["news_market"]
    market_code: NewsMarketCode


class MacroDashboardRunCreate(_DataManagementRunCreate):
    operation: Literal["macro_dashboard"]
    market_code: None = None


DataManagementRunCreate = Annotated[
    MorningAllRunCreate
    | MorningMarketRunCreate
    | IndexYahooRunCreate
    | InstitutionalTwseRunCreate
    | NewsAllRunCreate
    | NewsMarketRunCreate
    | MacroDashboardRunCreate,
    Field(discriminator="operation"),
]


class DataManagementCatalog(BaseModel):
    taipei_date: date
    morning_reports_enabled: bool
    yfinance_enabled: bool
    twse_enabled: bool
    markets: list[LaunchMarketCode]
    daily_news_enabled: bool
    news_markets: list[NewsMarketCode]
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


class NewsAllRunResponse(_DataManagementRunResponse):
    operation: Literal["news_all"]
    market_code: None


class NewsMarketRunResponse(_DataManagementRunResponse):
    operation: Literal["news_market"]
    market_code: NewsMarketCode


class NewsPublishRunResponse(_DataManagementRunResponse):
    """A manual publish of admin-chosen news candidates; created only through
    the news management API, never through the generic run endpoint."""

    operation: Literal["news_publish"]
    market_code: None


class MacroDashboardRunResponse(_DataManagementRunResponse):
    operation: Literal["macro_dashboard"]
    market_code: None


DataManagementRunResponse = Annotated[
    MorningAllRunResponse
    | MorningMarketRunResponse
    | IndexYahooRunResponse
    | InstitutionalTwseRunResponse
    | NewsAllRunResponse
    | NewsMarketRunResponse
    | NewsPublishRunResponse
    | MacroDashboardRunResponse,
    Field(discriminator="operation"),
]


class DataManagementRunList(BaseModel):
    items: list[DataManagementRunResponse]


def run_response(run: DataManagementRun) -> DataManagementRunResponse:
    values = dict(
        id=run.id,
        edition_date=run.edition_date,
        status=run.status,
        requested_by_user_id=run.requested_by_user_id,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
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
    if run.operation == "news_all":
        return NewsAllRunResponse(operation="news_all", market_code=None, **values)
    if run.operation == "news_market":
        return NewsMarketRunResponse(
            operation="news_market", market_code=cast(str, run.market_code), **values
        )
    if run.operation == "news_publish":
        return NewsPublishRunResponse(operation="news_publish", market_code=None, **values)
    if run.operation == "macro_dashboard":
        return MacroDashboardRunResponse(operation="macro_dashboard", market_code=None, **values)
    return IndexYahooRunResponse(operation="index_yahoo", market_code=None, **values)
