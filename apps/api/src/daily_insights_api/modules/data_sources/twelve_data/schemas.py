from datetime import date
from datetime import datetime as DateTime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TwelveDataModel(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class TwelveDataMeta(TwelveDataModel):
    symbol: str
    interval: str
    currency: str | None = None
    exchange_timezone: str
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


class TwelveDataQuote(TwelveDataModel):
    symbol: str
    name: str | None = None
    exchange: str | None = None
    mic_code: str | None = None
    currency: str | None = None
    datetime: DateTime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None = Field(default=None, ge=0)
    previous_close: Decimal | None = None
    change: Decimal | None = None
    percent_change: Decimal | None = None

    @field_validator("datetime")
    @classmethod
    def datetime_must_be_aware(cls, value: DateTime) -> DateTime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("quote datetime must include a UTC offset")
        return value


class TwelveDataMover(TwelveDataModel):
    symbol: str
    name: str
    exchange: str
    mic_code: str
    datetime: DateTime
    last: Decimal
    high: Decimal
    low: Decimal
    volume: int = Field(ge=0)
    change: Decimal
    percent_change: Decimal

    @field_validator("datetime")
    @classmethod
    def datetime_must_be_aware(cls, value: DateTime) -> DateTime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("mover datetime must include a UTC offset")
        return value


class TwelveDataMovers(TwelveDataModel):
    values: list[TwelveDataMover]
    status: str
