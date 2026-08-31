import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ValidationError

from daily_insights_api.modules.data_sources.dto import DailyBar, MarketCode, Provenance
from daily_insights_api.modules.data_sources.errors import DataSourceContractError
from daily_insights_api.modules.data_sources.twelve_data.schemas import (
    TwelveDataMovers,
    TwelveDataQuote,
    TwelveDataTimeSeries,
)
from daily_insights_api.modules.data_sources.twelve_data.transport import (
    QueryValue,
    TwelveDataTransport,
    TwelveDataTransportResponse,
)

TWELVE_DATA_CONTRACT_VERSION = "2026-08-31.v3"
TWELVE_DATA_CONTRACT_HASH = hashlib.sha256(
    b"twelve-data:quote,time_series,market_movers/stocks,asset-type:2026-08-31.v3"
).hexdigest()
TWELVE_DATA_CURRENCY_NAMES = {"USD": "US Dollar"}


@dataclass(frozen=True, slots=True)
class QuoteResult:
    symbol: str
    name: str | None
    currency: str
    as_of: date
    close: Decimal
    open: Decimal
    high: Decimal
    low: Decimal
    volume: int | None
    change: Decimal | None
    percent_change: Decimal | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class DailyBarsResult:
    items: tuple[DailyBar, ...]
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class Mover:
    symbol: str
    name: str
    as_of: date
    close: Decimal
    high: Decimal
    low: Decimal
    volume: int
    change: Decimal
    percent_change: Decimal


@dataclass(frozen=True, slots=True)
class MoversResult:
    items: tuple[Mover, ...]
    provenance: Provenance


class TwelveDataAdapter:
    def __init__(self, transport: TwelveDataTransport) -> None:
        self._transport = transport

    async def get_quote(
        self,
        *,
        market: MarketCode,
        symbol: str,
        expected_currency: str,
    ) -> QuoteResult:
        params: dict[str, QueryValue] = {"symbol": symbol}
        response = await self._transport.get("/quote", params=params)
        payload = _parse(response, TwelveDataQuote, "/quote")
        if payload.symbol != symbol:
            raise DataSourceContractError("Twelve Data quote symbol did not match the request")
        if payload.percent_change is None:
            raise DataSourceContractError("Twelve Data quote omitted a required field")
        currency = payload.currency or _quote_currency(symbol)
        if currency is None or re.fullmatch(r"[A-Z]{3}", currency) is None:
            raise DataSourceContractError("Twelve Data quote returned an invalid currency unit")
        if currency != expected_currency:
            raise DataSourceContractError(
                "Twelve Data quote currency did not match the launch manifest"
            )
        as_of = datetime.fromtimestamp(payload.timestamp, UTC).date()
        return QuoteResult(
            symbol=payload.symbol,
            name=payload.name,
            currency=currency,
            as_of=as_of,
            close=payload.close,
            open=payload.open,
            high=payload.high,
            low=payload.low,
            volume=payload.volume,
            change=payload.change,
            percent_change=payload.percent_change,
            provenance=_provenance(response, "/quote", params, as_of, 1),
        )

    async def get_daily_bars(
        self,
        *,
        market: MarketCode,
        symbol: str,
        expected_currency: str,
        outputsize: int,
        expected_asset_type: str | None = None,
    ) -> DailyBarsResult:
        if not 1 <= outputsize <= 5_000:
            raise ValueError("outputsize must be between 1 and 5000")
        params: dict[str, QueryValue] = {
            "symbol": symbol,
            "interval": "1day",
            "outputsize": outputsize,
            "order": "ASC",
        }
        response = await self._transport.get("/time_series", params=params)
        payload = _parse(response, TwelveDataTimeSeries, "/time_series")
        if payload.status != "ok" or payload.meta.symbol != symbol:
            raise DataSourceContractError(
                "Twelve Data time series was unsuccessful or returned another symbol"
            )
        if payload.meta.interval != "1day":
            raise DataSourceContractError("Twelve Data returned an unexpected interval")
        expected_currency_name = TWELVE_DATA_CURRENCY_NAMES.get(expected_currency)
        if expected_currency_name is None:
            raise ValueError("expected_currency is not supported by the Twelve Data contract")
        if payload.meta.currency_quote != expected_currency_name:
            raise DataSourceContractError(
                "Twelve Data time-series quote currency did not match the launch manifest"
            )
        if expected_asset_type is not None and payload.meta.type != expected_asset_type:
            raise DataSourceContractError(
                "Twelve Data time-series asset type did not match the launch manifest"
            )
        items = tuple(
            DailyBar(
                instrument_source_id=symbol,
                market=market,
                symbol=symbol,
                trade_date=item.datetime,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume,
                source="twelve_data",
            )
            for item in payload.values
        )
        if not items:
            raise DataSourceContractError("Twelve Data returned an empty time series")
        if len(items) < outputsize:
            raise DataSourceContractError("Twelve Data returned less than the required history")
        if any(
            value is None
            for item in items
            for value in (item.open, item.high, item.low, item.close)
        ):
            raise DataSourceContractError("Twelve Data daily bars omitted a required field")
        if any(left.trade_date >= right.trade_date for left, right in pairwise(items)):
            raise DataSourceContractError("Twelve Data time series must be strictly ascending")
        as_of = items[-1].trade_date
        return DailyBarsResult(
            items=items,
            provenance=_provenance(response, "/time_series", params, as_of, len(items)),
        )

    async def get_stock_movers(
        self,
        *,
        direction: Literal["gainers", "losers"],
        outputsize: int,
    ) -> MoversResult:
        if not 1 <= outputsize <= 50:
            raise ValueError("outputsize must be between 1 and 50")
        params: dict[str, QueryValue] = {
            "direction": direction,
            "outputsize": outputsize,
            "country": "USA",
        }
        response = await self._transport.get("/market_movers/stocks", params=params)
        payload = _parse(response, TwelveDataMovers, "/market_movers/stocks")
        if payload.status != "ok" or len(payload.values) != outputsize:
            raise DataSourceContractError("Twelve Data returned an incomplete movers result")
        items = tuple(
            Mover(
                symbol=item.symbol,
                name=item.name,
                as_of=item.market_date,
                close=item.last,
                high=item.high,
                low=item.low,
                volume=item.volume,
                change=item.change,
                percent_change=item.percent_change,
            )
            for item in payload.values
        )
        as_of = min(item.as_of for item in items)
        return MoversResult(
            items=items,
            provenance=_provenance(
                response,
                "/market_movers/stocks",
                params,
                as_of,
                len(items),
            ),
        )


def _quote_currency(symbol: str) -> str | None:
    parts = symbol.split("/", maxsplit=1)
    return parts[1] if len(parts) == 2 else None


def _parse[PayloadT: BaseModel](
    response: TwelveDataTransportResponse,
    payload_type: type[PayloadT],
    endpoint: str,
) -> PayloadT:
    try:
        return payload_type.model_validate_json(response.content)
    except ValidationError as error:
        raise DataSourceContractError(
            f"Twelve Data response no longer matches the reviewed contract for {endpoint}"
        ) from error


def _provenance(
    response: TwelveDataTransportResponse,
    endpoint: str,
    params: dict[str, QueryValue],
    as_of: date,
    record_count: int,
) -> Provenance:
    query = json.dumps(sorted(params.items()), separators=(",", ":")).encode()
    return Provenance(
        provider="twelve_data",
        contract_version=TWELVE_DATA_CONTRACT_VERSION,
        contract_hash=TWELVE_DATA_CONTRACT_HASH,
        endpoint=endpoint,
        query_fingerprint=hashlib.sha256(query).hexdigest(),
        fetched_at=response.fetched_at,
        as_of=as_of,
        response_digest=response.response_digest,
        record_count=record_count,
        request_id=response.request_id,
    )
