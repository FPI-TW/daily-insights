from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class MarketResponse(BaseModel):
    code: str
    name_en: str
    name_zh_hant: str
    name_zh_hans: str
    is_visible: bool


class IndexDailyBarResponse(BaseModel):
    symbol: str
    market_code: str
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal
    volume: int | None


class IndexLatestBarResponse(IndexDailyBarResponse):
    """The most recent settled bar plus the close before it, for a change figure."""

    previous_close: Decimal | None
