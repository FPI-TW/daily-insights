from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, PlainSerializer, WithJsonSchema

# A stored zero comes back from Numeric(20,10) as Decimal("0E-10"), and Pydantic
# would put that exponent form on the wire. It fails the pattern this schema
# itself declares in the OpenAPI document, and every client that reads these as
# decimal strings, so one zero open would break the whole bar list. Yahoo does
# return Open=0 on old rows, and nothing upstream rejects it: the adapter and
# the table only constrain close.
PriceDecimal = Annotated[
    Decimal,
    PlainSerializer(lambda value: format(value, "f"), return_type=str),
    # Pydantic's own Decimal pattern went on the wire while describing a format
    # Pydantic did not emit. This one states what these fields actually are, and
    # matches the check the generated client applies.
    WithJsonSchema({"type": "string", "pattern": r"^-?\d+(?:\.\d+)?$"}, mode="serialization"),
]


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
    open: PriceDecimal | None
    high: PriceDecimal | None
    low: PriceDecimal | None
    close: PriceDecimal
    volume: int | None


class IndexLatestBarResponse(IndexDailyBarResponse):
    """The most recent settled bar plus the close before it, for a change figure."""

    previous_close: PriceDecimal | None


class IndexMovingAveragePointResponse(BaseModel):
    trade_date: date
    value: PriceDecimal | None


class IndexMovingAverage20SeriesResponse(BaseModel):
    period: Literal[20]
    points: list[IndexMovingAveragePointResponse]


class IndexMovingAverage60SeriesResponse(BaseModel):
    period: Literal[60]
    points: list[IndexMovingAveragePointResponse]


class IndexMovingAverage120SeriesResponse(BaseModel):
    period: Literal[120]
    points: list[IndexMovingAveragePointResponse]


class IndexMovingAverage240SeriesResponse(BaseModel):
    period: Literal[240]
    points: list[IndexMovingAveragePointResponse]


class IndexMovingAveragesResponse(BaseModel):
    """Read-time simple moving averages of settled daily closing prices."""

    symbol: str
    market_code: str
    method: Literal["sma"]
    price_field: Literal["close"]
    formula_version: Literal["sma-close-v1"]
    as_of: date | None
    series: tuple[
        IndexMovingAverage20SeriesResponse,
        IndexMovingAverage60SeriesResponse,
        IndexMovingAverage120SeriesResponse,
        IndexMovingAverage240SeriesResponse,
    ]


class InstitutionalMarketFlowResponse(BaseModel):
    """One trading day of whole-market net amounts (TWD).

    The five stored investor categories are folded into the three the product
    talks about: the two dealer books are one desk, and the foreign dealer is
    still foreign money.
    """

    trade_date: date
    foreign: int
    trust: int
    dealer: int


class InstitutionalStockFlowLeaderResponse(BaseModel):
    """One security's net shares for one day, summed over all five investors.

    The day is on the envelope, which is also where it lives when both lists
    are empty, so a row does not repeat it.
    """

    symbol: str
    security_name: str
    net_shares: int


class InstitutionalStockFlowLeadersResponse(BaseModel):
    """The largest net buys and net sells of one trading day, at most five of
    each: a security is only listed on the side its total actually falls on."""

    trade_date: date | None
    top_buys: list[InstitutionalStockFlowLeaderResponse]
    top_sells: list[InstitutionalStockFlowLeaderResponse]
