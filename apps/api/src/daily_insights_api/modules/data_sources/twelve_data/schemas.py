from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class TwelveDataModel(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class TwelveDataMeta(TwelveDataModel):
    symbol: str
    interval: str
    currency: str | None = None
    currency_base: str | None = None
    currency_quote: str | None = None
    exchange_timezone: str | None = None
    exchange: str | None = None
    mic_code: str | None = None
    type: str | None = None


class TwelveDataBar(TwelveDataModel):
    datetime: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None = Field(default=None, ge=0)


class TwelveDataTimeSeries(TwelveDataModel):
    meta: TwelveDataMeta
    values: list[TwelveDataBar]
    status: str


class TwelveDataEod(TwelveDataModel):
    """Reviewed payload returned by Twelve Data's commodity ``/eod`` endpoint."""

    symbol: str
    exchange: str
    currency: str | None = None
    datetime: date
    close: Decimal
