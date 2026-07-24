from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
)


def _require_json_string(value: object) -> object:
    if not isinstance(value, str):
        raise ValueError("FinDB contract requires a JSON string")
    return value


FinDBUUID = Annotated[UUID, BeforeValidator(_require_json_string)]
FinDBDate = Annotated[date, BeforeValidator(_require_json_string)]
FinDBDateTime = Annotated[datetime, BeforeValidator(_require_json_string)]
FinDBDecimal = Annotated[Decimal, BeforeValidator(_require_json_string)]
PositiveStrictInt = Annotated[int, Field(strict=True, ge=1)]
NonNegativeStrictInt = Annotated[int, Field(strict=True, ge=0)]


class FinDBRawModel(BaseModel):
    # Upstream may add fields without breaking consumers. Required known fields
    # and their types remain strictly validated below.
    model_config = ConfigDict(extra="allow")


class FinDBPagination(FinDBRawModel):
    page: PositiveStrictInt
    page_size: PositiveStrictInt
    total_records: NonNegativeStrictInt | None
    total_pages: NonNegativeStrictInt | None
    next_cursor: str | None = None


class FinDBInstrument(FinDBRawModel):
    instrument_id: FinDBUUID
    asset_class: str
    market: str
    symbol: str
    status: str
    name: str | None = None
    currency: str | None = None
    timezone: str | None = None
    listed_date: FinDBDate | None = None
    delisted_date: FinDBDate | None = None
    first_trade_date: FinDBDate | None = None
    latest_trade_date: FinDBDate | None = None
    latest_price: FinDBDecimal | None = None


class FinDBDailyBar(FinDBRawModel):
    instrument_id: FinDBUUID
    symbol: str
    market: str
    trade_date: FinDBDate
    name: str | None = None
    open: FinDBDecimal | None = None
    high: FinDBDecimal | None = None
    low: FinDBDecimal | None = None
    close: FinDBDecimal | None = None
    volume: StrictInt | None = None
    total_ticks: StrictInt | None = None
    turnover: FinDBDecimal | None = None
    source: str | None = None
    created_at: FinDBDateTime | None = None
    updated_at: FinDBDateTime | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def datetimes_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("FinDB datetimes must include a UTC offset")
        return value


class FinDBPage[RawItemT](FinDBRawModel):
    success: StrictBool
    data: list[RawItemT]
    pagination: FinDBPagination
