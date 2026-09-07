import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

MAX_QUOTE_TIMESTAMP = 4_102_444_800  # 2100-01-01T00:00:00Z
QUOTE_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


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


class TwelveDataQuote(TwelveDataModel):
    symbol: str
    name: str | None = None
    exchange: str | None = None
    mic_code: str | None = None
    currency: str | None = None
    datetime: StrictStr
    timestamp: StrictInt = Field(ge=0, le=MAX_QUOTE_TIMESTAMP)
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
    def datetime_must_be_calendar_date(cls, value: str) -> str:
        if QUOTE_DATE_PATTERN.fullmatch(value) is None:
            raise ValueError("quote datetime must use YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("quote datetime must be a valid calendar date") from error
        return value


class TwelveDataEod(TwelveDataModel):
    """Reviewed payload returned by Twelve Data's commodity ``/eod`` endpoint."""

    symbol: str
    exchange: str
    currency: str | None = None
    datetime: date
    close: Decimal
