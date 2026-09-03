from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class UpstreamSummary(BaseModel):
    """The only accepted upstream response shape for GET /api/summary."""

    model_config = ConfigDict(extra="forbid")

    us_macro: list[str]
    forex: list[str]
    crypto: list[str]
    us_stocks: list[str]
    hk_stocks: list[str]
    cn_stocks: list[str]
    tw_stocks: list[str]
    tw_futures: list[str]

    @field_validator(
        "us_macro",
        "forex",
        "crypto",
        "us_stocks",
        "hk_stocks",
        "cn_stocks",
        "tw_stocks",
        "tw_futures",
    )
    @classmethod
    def normalize_points(cls, values: list[str]) -> list[str]:
        """Blank upstream strings carry no viewpoint and must never overwrite one."""

        return [value.strip() for value in values if value.strip()]


class AnalystViewpointResponse(BaseModel):
    viewpoint_date: date
    market_code: str
    source_market_code: str
    points: list[str]
    fetched_at: datetime


class SyncMarketStatus(BaseModel):
    source_market_code: str
    market_code: str
    status: Literal["updated", "missing", "stale"]


class AnalystViewpointSyncResponse(BaseModel):
    viewpoint_date: date
    fetched_at: datetime
    status: Literal["complete", "partial"]
    markets: list[SyncMarketStatus]


class AnalystViewpointExecutionResponse(BaseModel):
    viewpoint_date: date
    trigger: Literal["scheduler", "manual"]
    status: Literal["complete", "partial", "failed"]
    fetched_at: datetime | None
    completed_at: datetime
    error_code: str | None
    markets: list[SyncMarketStatus]


class AnalystViewpointSyncStatusResponse(BaseModel):
    enabled: bool
    today: date
    viewpoints: list[AnalystViewpointResponse]
    latest_sync: AnalystViewpointExecutionResponse | None
