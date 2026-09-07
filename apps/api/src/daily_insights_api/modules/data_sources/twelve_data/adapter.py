import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise

from pydantic import BaseModel, TypeAdapter, ValidationError

from daily_insights_api.modules.data_sources.dto import DailyBar, MarketCode, Provenance
from daily_insights_api.modules.data_sources.errors import DataSourceContractError
from daily_insights_api.modules.data_sources.twelve_data.schemas import (
    TwelveDataEod,
    TwelveDataQuote,
    TwelveDataTimeSeries,
)
from daily_insights_api.modules.data_sources.twelve_data.transport import (
    QueryValue,
    TwelveDataTransport,
    TwelveDataTransportResponse,
)

TWELVE_DATA_CONTRACT_VERSION = "2026-09-07.v5"
TWELVE_DATA_CONTRACT_HASH = hashlib.sha256(
    b"twelve-data:quote,eod,time_series,market_movers/stocks,commodity-eod:2026-09-07.v5"
).hexdigest()
# Commodity 1day metadata is inconsistent: most USD commodities spell out
# "US Dollar", while HG1 (with type=commodity) returns the ISO code. Both
# are reviewed representations of the same manifest currency; no other alias
# is accepted at this trust boundary.
TWELVE_DATA_CURRENCY_NAMES = {"USD": frozenset(("US Dollar", "USD"))}


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
    previous_close: Decimal | None
    change: Decimal | None
    # The provider's own figure, kept for provenance; reports derive their
    # change from previous_close so the definition is ours.
    percent_change: Decimal | None
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class QuotesResult:
    """Quotes in the requested symbol order plus one provenance per request made."""

    items: tuple[QuoteResult, ...]
    provenances: tuple[Provenance, ...]


@dataclass(frozen=True, slots=True)
class DailyBarsResult:
    items: tuple[DailyBar, ...]
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class EodResult:
    symbol: str
    currency: str
    as_of: date
    close: Decimal
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class EodsResult:
    """EOD values in the requested symbol order from one provider batch."""

    items: tuple[EodResult, ...]
    provenance: Provenance


_BATCH_QUOTES = TypeAdapter(dict[str, TwelveDataQuote])
_BATCH_EODS = TypeAdapter(dict[str, TwelveDataEod])


class TwelveDataAdapter:
    def __init__(self, transport: TwelveDataTransport) -> None:
        self._transport = transport

    async def get_quote(
        self,
        *,
        market: MarketCode,
        symbol: str,
        expected_currency: str,
        symbol_type: str | None = None,
    ) -> QuoteResult:
        result = await self.get_quotes(
            market=market,
            symbols=(symbol,),
            expected_currencies={symbol: expected_currency},
            symbol_types={symbol: symbol_type} if symbol_type else {},
        )
        return result.items[0]

    async def get_quotes(
        self,
        *,
        market: MarketCode,
        symbols: tuple[str, ...],
        expected_currencies: dict[str, str],
        symbol_types: dict[str, str] | None = None,
    ) -> QuotesResult:
        """Fetch several quotes with one request per asset-class group.

        The provider's ``type`` parameter applies to a whole request, so
        symbols that pin a type (commodities) are requested apart from the
        rest. Every quote passes the same contract checks as a single request.
        """
        del market
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be a non-empty tuple of distinct symbols")
        if set(expected_currencies) != set(symbols):
            raise ValueError("expected_currencies must cover every requested symbol")
        types = symbol_types or {}
        groups: dict[str | None, list[str]] = {}
        for symbol in symbols:
            groups.setdefault(types.get(symbol), []).append(symbol)
        quotes: dict[str, QuoteResult] = {}
        provenances: list[Provenance] = []
        for symbol_type, group in groups.items():
            params: dict[str, QueryValue] = {"symbol": ",".join(group)}
            if symbol_type is not None:
                params["type"] = symbol_type
            response = await self._transport.get("/quote", params=params)
            payloads = _parse_quotes(response, group)
            parsed = {
                symbol: _quote_result(
                    payloads[symbol],
                    symbol,
                    expected_currencies[symbol],
                    symbol_type,
                    response,
                    params,
                )
                for symbol in group
            }
            quotes.update(parsed)
            provenances.append(
                _provenance(
                    response,
                    "/quote",
                    params,
                    min(item.as_of for item in parsed.values()),
                    len(parsed),
                )
            )
        return QuotesResult(
            items=tuple(quotes[symbol] for symbol in symbols),
            provenances=tuple(provenances),
        )

    async def get_daily_bars(
        self,
        *,
        market: MarketCode,
        symbol: str,
        expected_currency: str,
        outputsize: int,
        expected_asset_type: str | None = None,
        symbol_type: str | None = None,
        dp: int | None = None,
    ) -> DailyBarsResult:
        if not 1 <= outputsize <= 5_000:
            raise ValueError("outputsize must be between 1 and 5000")
        if dp is not None and not 0 <= dp <= 11:
            raise ValueError("dp must be between 0 and 11")
        params: dict[str, QueryValue] = {
            "symbol": symbol,
            "interval": "1day",
            "outputsize": outputsize,
            "order": "ASC",
        }
        if symbol_type is not None:
            params["type"] = symbol_type
        if dp is not None:
            params["dp"] = dp
        response = await self._transport.get("/time_series", params=params)
        payload = _parse(response, TwelveDataTimeSeries, "/time_series")
        if payload.status != "ok" or payload.meta.symbol != symbol:
            raise DataSourceContractError(
                "Twelve Data time series was unsuccessful or returned another symbol"
            )
        if payload.meta.interval != "1day":
            raise DataSourceContractError("Twelve Data returned an unexpected interval")
        expected_currency_names = TWELVE_DATA_CURRENCY_NAMES.get(expected_currency)
        if expected_currency_names is None:
            raise ValueError("expected_currency is not supported by the Twelve Data contract")
        if payload.meta.currency_quote not in expected_currency_names:
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

    async def get_eods(
        self,
        *,
        market: MarketCode,
        symbols: tuple[str, ...],
        expected_currencies: dict[str, str],
    ) -> EodsResult:
        """Fetch commodity EOD closes in one exact-coverage request.

        Twelve Data omits commodity currencies in this response. The immutable
        launch manifest is therefore the authority for the displayed unit.
        """
        del market
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be a non-empty tuple of distinct symbols")
        if set(expected_currencies) != set(symbols):
            raise ValueError("expected_currencies must cover every requested symbol")
        if any(currency != "USD" for currency in expected_currencies.values()):
            raise ValueError("commodity EOD currency must be pinned to USD")
        params: dict[str, QueryValue] = {
            "symbol": ",".join(symbols),
            "type": "commodity",
            "dp": 11,
        }
        response = await self._transport.get("/eod", params=params)
        payloads = _parse_eods(response, symbols)
        items = tuple(
            _eod_result(
                payloads[symbol],
                symbol,
                expected_currencies[symbol],
                response,
                params,
            )
            for symbol in symbols
        )
        return EodsResult(
            items=items,
            provenance=_provenance(
                response,
                "/eod",
                params,
                min(item.as_of for item in items),
                len(items),
            ),
        )


def _quote_currency(symbol: str) -> str | None:
    parts = symbol.split("/", maxsplit=1)
    return parts[1] if len(parts) == 2 else None


def _parse_quotes(
    response: TwelveDataTransportResponse, symbols: list[str]
) -> dict[str, TwelveDataQuote]:
    """A single-symbol request answers with one flat quote; a batch answers with
    a symbol-keyed object whose entries may individually be error objects,
    which fail validation and therefore the whole request."""
    if len(symbols) == 1:
        return {symbols[0]: _parse(response, TwelveDataQuote, "/quote")}
    try:
        payloads = _BATCH_QUOTES.validate_json(response.content)
    except ValidationError as error:
        raise DataSourceContractError(
            "Twelve Data response no longer matches the reviewed contract for /quote"
        ) from error
    if set(payloads) != set(symbols):
        raise DataSourceContractError("Twelve Data batch quote did not cover every symbol")
    return payloads


def _parse_eods(
    response: TwelveDataTransportResponse, symbols: tuple[str, ...]
) -> dict[str, TwelveDataEod]:
    if len(symbols) == 1:
        return {symbols[0]: _parse(response, TwelveDataEod, "/eod")}
    try:
        payloads = _BATCH_EODS.validate_json(response.content)
    except ValidationError as error:
        raise DataSourceContractError(
            "Twelve Data response no longer matches the reviewed contract for /eod"
        ) from error
    if set(payloads) != set(symbols):
        raise DataSourceContractError("Twelve Data batch EOD did not cover every symbol")
    return payloads


def _quote_result(
    payload: TwelveDataQuote,
    symbol: str,
    expected_currency: str,
    symbol_type: str | None,
    response: TwelveDataTransportResponse,
    params: dict[str, QueryValue],
) -> QuoteResult:
    if payload.symbol != symbol:
        raise DataSourceContractError("Twelve Data quote symbol did not match the request")
    if payload.previous_close is None or payload.previous_close <= 0:
        raise DataSourceContractError("Twelve Data quote omitted a usable previous close")
    currency = payload.currency or _quote_currency(symbol)
    # Commodity quotes carry no currency field; the manifest pins their unit,
    # and the type parameter already guarantees the asset class.
    if currency is None and symbol_type == "commodity":
        currency = expected_currency
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
        previous_close=payload.previous_close,
        change=payload.change,
        percent_change=payload.percent_change,
        provenance=_provenance(response, "/quote", params, as_of, 1),
    )


def _eod_result(
    payload: TwelveDataEod,
    symbol: str,
    expected_currency: str,
    response: TwelveDataTransportResponse,
    params: dict[str, QueryValue],
) -> EodResult:
    if payload.symbol != symbol:
        raise DataSourceContractError("Twelve Data EOD symbol did not match the request")
    if payload.currency is not None and payload.currency != expected_currency:
        raise DataSourceContractError("Twelve Data EOD currency did not match the launch manifest")
    if payload.close <= 0:
        raise DataSourceContractError("Twelve Data EOD close must be positive")
    return EodResult(
        symbol=symbol,
        currency=expected_currency,
        as_of=payload.datetime,
        close=payload.close,
        provenance=_provenance(response, "/eod", params, payload.datetime, 1),
    )


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
