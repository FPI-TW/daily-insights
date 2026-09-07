import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from daily_insights_api.modules.reports.api import LaunchMarketCode

NewsMarketCode = Literal["global", "tw_equity", "us_equity"]
RunOperation = Literal[
    "morning_all",
    "morning_market",
    "index_yahoo",
    "institutional_twse",
    "news_all",
    "news_market",
]
RunOperationGroup = Literal["news"]
RunStatus = Literal["pending", "running", "succeeded", "partial", "failed"]


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


DataManagementRunCreate = Annotated[
    MorningAllRunCreate
    | MorningMarketRunCreate
    | IndexYahooRunCreate
    | InstitutionalTwseRunCreate
    | NewsAllRunCreate
    | NewsMarketRunCreate,
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


DataManagementRunResponse = Annotated[
    MorningAllRunResponse
    | MorningMarketRunResponse
    | IndexYahooRunResponse
    | InstitutionalTwseRunResponse
    | NewsAllRunResponse
    | NewsMarketRunResponse,
    Field(discriminator="operation"),
]


class DataManagementRunList(BaseModel):
    items: list[DataManagementRunResponse]
