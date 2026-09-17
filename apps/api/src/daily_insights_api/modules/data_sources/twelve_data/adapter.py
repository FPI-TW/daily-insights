import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

from pydantic import BaseModel, TypeAdapter, ValidationError

from daily_insights_api.modules.data_sources.dto import DailyBar, MarketCode, Provenance
from daily_insights_api.modules.data_sources.errors import DataSourceContractError
from daily_insights_api.modules.data_sources.twelve_data.schemas import (
    TwelveDataEod,
    TwelveDataTimeSeries,
)
from daily_insights_api.modules.data_sources.twelve_data.transport import (
    QueryValue,
    TwelveDataTransport,
    TwelveDataTransportResponse,
)

TWELVE_DATA_CONTRACT_VERSION = "2026-09-16.v6"
TWELVE_DATA_CONTRACT_HASH = hashlib.sha256(
    b"twelve-data:eod,time_series,completed-daily-bars:2026-09-16.v6"
).hexdigest()
# Commodity 1day metadata is inconsistent: most USD commodities spell out
# "US Dollar", while HG1 (with type=commodity) returns the ISO code. Both
# are reviewed representations of the same manifest currency; no other alias
# is accepted at this trust boundary.
TWELVE_DATA_CURRENCY_NAMES = {
    "USD": frozenset(("US Dollar", "USD")),
    "JPY": frozenset(("Japanese Yen", "JPY")),
    "CHF": frozenset(("Swiss Franc", "CHF")),
    "CAD": frozenset(("Canadian Dollar", "CAD")),
    "TWD": frozenset(("Taiwan Dollar", "TWD")),
    "KRW": frozenset(("Korean Won", "KRW")),
    "HKD": frozenset(("Hong Kong Dollar", "HKD")),
    "CNH": frozenset(("Chinese Yuan (Offshore)", "CNH")),
    "SGD": frozenset(("Singapore Dollar", "SGD")),
    "GBP": frozenset(("British Pound", "GBP")),
}


@dataclass(frozen=True, slots=True)
class CompletedPriceResult:
    symbol: str
    currency: str
    as_of: date
    close: Decimal
    previous_close: Decimal
    bars: tuple[DailyBar, ...]
    provenances: tuple[Provenance, ...]


@dataclass(frozen=True, slots=True)
class CompletedPricesResult:
    """Completed closes in requested order, anchored by official EOD."""

    items: tuple[CompletedPriceResult, ...]
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


_BATCH_EODS = TypeAdapter(dict[str, TwelveDataEod])


class TwelveDataAdapter:
    def __init__(self, transport: TwelveDataTransport) -> None:
        self._transport = transport

    async def get_completed_prices(
        self,
        *,
        market: MarketCode,
        symbols: tuple[str, ...],
        expected_currencies: dict[str, str],
        symbol_types: dict[str, str] | None = None,
        expected_asset_types: dict[str, str] | None = None,
        outputsize: int = 2,
    ) -> CompletedPricesResult:
        """Return only completed sessions and cross-check latest `/eod`.

        `/time_series` supplies both the current and previous completed
        sessions. `/eod` validates only the latest official close because it
        has no historical observation in its contract.
        """
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be a non-empty tuple of distinct symbols")
        if set(expected_currencies) != set(symbols):
            raise ValueError("expected_currencies must cover every requested symbol")
        types = symbol_types or {}
        groups: dict[str | None, list[str]] = {}
        for symbol in symbols:
            groups.setdefault(types.get(symbol), []).append(symbol)
        eods: dict[str, EodResult] = {}
        provenances: list[Provenance] = []
        for symbol_type, group in groups.items():
            group_eods = await self.get_eods(
                market=market,
                symbols=tuple(group),
                expected_currencies={symbol: expected_currencies[symbol] for symbol in group},
                symbol_type=symbol_type,
            )
            eods.update({item.symbol: item for item in group_eods.items})
            provenances.append(group_eods.provenance)
        results: dict[str, CompletedPriceResult] = {}
        for symbol in symbols:
            series = await self.get_daily_bars(
                market=market,
                symbol=symbol,
                expected_currency=expected_currencies[symbol],
                outputsize=max(2, outputsize),
                expected_asset_type=(expected_asset_types or {}).get(symbol),
                symbol_type=types.get(symbol),
                minimum_items=2,
            )
            completed = tuple(
                item for item in series.items if item.trade_date <= eods[symbol].as_of
            )
            if len(completed) < 2:
                raise DataSourceContractError(
                    "Twelve Data returned fewer than two completed sessions"
                )
            latest = completed[-1]
            if latest.trade_date != eods[symbol].as_of or latest.close != eods[symbol].close:
                raise DataSourceContractError(
                    "Twelve Data EOD date or close did not match the daily series"
                )
            assert latest.close is not None and completed[-2].close is not None
            results[symbol] = CompletedPriceResult(
                symbol=symbol,
                currency=expected_currencies[symbol],
                as_of=latest.trade_date,
                close=latest.close,
                previous_close=completed[-2].close,
                bars=completed,
                provenances=(eods[symbol].provenance, series.provenance),
            )
            provenances.append(series.provenance)
        return CompletedPricesResult(
            items=tuple(results[symbol] for symbol in symbols),
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
        end_date: date | None = None,
        timezone: str | None = None,
        minimum_items: int | None = None,
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
        if end_date is not None:
            params["end_date"] = end_date.isoformat()
        if timezone is not None:
            params["timezone"] = timezone
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
        actual_currency = payload.meta.currency_quote or payload.meta.currency
        if actual_currency not in expected_currency_names:
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
        required_items = outputsize if minimum_items is None else minimum_items
        if required_items < 1 or required_items > outputsize:
            raise ValueError("minimum_items must be between 1 and outputsize")
        if len(items) < required_items:
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
        symbol_type: str | None = "commodity",
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
        params: dict[str, QueryValue] = {
            "symbol": ",".join(symbols),
            "dp": 11,
        }
        if symbol_type is not None:
            params["type"] = symbol_type
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
