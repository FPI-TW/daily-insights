from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MarketCode = Literal[
    "global_macro_bonds",
    "crypto",
    "forex",
    "us_equity",
    "hk_equity",
    "cn_equity",
    "tw_equity",
    "tw_index_derivatives",
]


class ImmutableDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InstrumentQuery(ImmutableDTO):
    market: MarketCode | None = None
    asset_class: str | None = None
    status: str | None = None
    symbol: str | None = None
    cursor: str | None = None
    include_count: bool = True
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=1000)


class DailyBarQuery(ImmutableDTO):
    market: MarketCode | None = None
    symbols: tuple[str, ...] = ()
    start_date: date | None = None
    end_date: date | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=1000)

    @field_validator("end_date")
    @classmethod
    def end_must_not_precede_start(cls, value: date | None, info: object) -> date | None:
        data = getattr(info, "data", {})
        start_date = data.get("start_date")
        if value is not None and isinstance(start_date, date) and value < start_date:
            raise ValueError("end_date must not precede start_date")
        return value


class Instrument(ImmutableDTO):
    source_id: str
    market: MarketCode
    asset_class: str
    symbol: str
    status: str
    name: str | None = None
    currency: str | None = None
    timezone: str | None = None
    listed_date: date | None = None
    delisted_date: date | None = None
    first_trade_date: date | None = None
    latest_trade_date: date | None = None
    latest_price: Decimal | None = None


class DailyBar(ImmutableDTO):
    instrument_source_id: str
    market: MarketCode
    symbol: str
    trade_date: date
    name: str | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None
    total_ticks: int | None = None
    turnover: Decimal | None = None
    source: str | None = None
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None

    @field_validator("source_created_at", "source_updated_at")
    @classmethod
    def source_datetimes_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("source datetimes must include a UTC offset")
        return value


class PageInfo(ImmutableDTO):
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_records: int | None
    total_pages: int | None
    next_cursor: str | None = None


class Provenance(ImmutableDTO):
    provider: Literal["findb"] = "findb"
    contract_version: str
    contract_hash: str
    endpoint: str
    query_fingerprint: str
    fetched_at: datetime
    as_of: date | None
    response_digest: str
    record_count: int = Field(ge=0)
    request_id: str | None = None

    @field_validator("fetched_at")
    @classmethod
    def fetched_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fetched_at must include a UTC offset")
        return value


class ProviderPage[ItemT](ImmutableDTO):
    items: tuple[ItemT, ...]
    pagination: PageInfo
    provenance: Provenance
