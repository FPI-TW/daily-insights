import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from datetime import date

from pydantic import ValidationError

from daily_insights_api.modules.data_sources.dto import (
    DailyBar,
    DailyBarQuery,
    Instrument,
    InstrumentQuery,
    MarketCode,
    PageInfo,
    Provenance,
    ProviderPage,
)
from daily_insights_api.modules.data_sources.errors import (
    DataSourceContractError,
    UnsupportedMarketError,
)
from daily_insights_api.modules.data_sources.findb.schemas import (
    FinDBDailyBar,
    FinDBInstrument,
    FinDBPage,
)
from daily_insights_api.modules.data_sources.findb.transport import (
    FinDBTransport,
    FinDBTransportResponse,
    QueryValue,
)

FINDB_CONTRACT_VERSION = "0.1.0"
FINDB_CONTRACT_HASH = "a5f299b27a8533de5c5e13cfec748172ab531e1dc7d32c3a1391dac5b4ac76a1"
INSTRUMENTS_ENDPOINT = "/api/v1/serve/instruments"
EOD_ENDPOINT = "/api/v1/serve/eod"

_INTERNAL_TO_FINDB_MARKET: dict[MarketCode, str] = {
    "us_equity": "US",
    "crypto": "CRYPTO",
}
_FINDB_TO_INTERNAL_MARKET = {
    provider_code: internal_code
    for internal_code, provider_code in _INTERNAL_TO_FINDB_MARKET.items()
}


class FinDBAdapter:
    def __init__(self, transport: FinDBTransport) -> None:
        self._transport = transport

    async def get_instruments(self, query: InstrumentQuery) -> ProviderPage[Instrument]:
        params = _instrument_params(query)
        payload = await self._transport.get(INSTRUMENTS_ENDPOINT, params=params)
        raw_page = _parse_page(payload, FinDBPage[FinDBInstrument], INSTRUMENTS_ENDPOINT)
        items = tuple(_map_instrument(item) for item in raw_page.data)
        as_of = max(
            (item.latest_trade_date for item in items if item.latest_trade_date is not None),
            default=None,
        )
        return ProviderPage(
            items=items,
            pagination=_page_info(raw_page),
            provenance=_provenance(
                payload,
                endpoint=INSTRUMENTS_ENDPOINT,
                params=params,
                as_of=as_of,
                record_count=len(items),
            ),
        )

    async def iter_instrument_pages(
        self,
        query: InstrumentQuery,
        *,
        max_pages: int = 1000,
    ) -> AsyncIterator[ProviderPage[Instrument]]:
        _validate_max_pages(max_pages)
        current = query
        for _ in range(max_pages):
            result = await self.get_instruments(current)
            yield result
            next_cursor = result.pagination.next_cursor
            if next_cursor is not None:
                current = current.model_copy(
                    update={"cursor": next_cursor, "page": current.page + 1}
                )
                continue
            if (
                result.pagination.total_pages is not None
                and current.page < result.pagination.total_pages
            ):
                current = current.model_copy(update={"cursor": None, "page": current.page + 1})
                continue
            return
        raise DataSourceContractError(
            f"FinDB instrument pagination exceeded the {max_pages}-page safety bound"
        )

    async def get_daily_bars(self, query: DailyBarQuery) -> ProviderPage[DailyBar]:
        params = _daily_bar_params(query)
        payload = await self._transport.get(EOD_ENDPOINT, params=params)
        raw_page = _parse_page(payload, FinDBPage[FinDBDailyBar], EOD_ENDPOINT)
        items = tuple(_map_daily_bar(item) for item in raw_page.data)
        as_of = max((item.trade_date for item in items), default=None)
        return ProviderPage(
            items=items,
            pagination=_page_info(raw_page),
            provenance=_provenance(
                payload,
                endpoint=EOD_ENDPOINT,
                params=params,
                as_of=as_of,
                record_count=len(items),
            ),
        )

    async def iter_daily_bar_pages(
        self,
        query: DailyBarQuery,
        *,
        max_pages: int = 1000,
    ) -> AsyncIterator[ProviderPage[DailyBar]]:
        _validate_max_pages(max_pages)
        current = query
        for _ in range(max_pages):
            result = await self.get_daily_bars(current)
            yield result
            total_pages = result.pagination.total_pages
            if total_pages is None or current.page >= total_pages:
                return
            current = current.model_copy(update={"page": current.page + 1})
        raise DataSourceContractError(
            f"FinDB EOD pagination exceeded the {max_pages}-page safety bound"
        )


def _parse_page[RawT](
    payload: FinDBTransportResponse,
    page_type: type[FinDBPage[RawT]],
    endpoint: str,
) -> FinDBPage[RawT]:
    try:
        page = page_type.model_validate_json(payload.content)
    except ValidationError as error:
        raise DataSourceContractError(
            f"FinDB response no longer matches the reviewed contract for {endpoint}"
        ) from error
    if not page.success:
        raise DataSourceContractError(f"FinDB reported an unsuccessful response for {endpoint}")
    return page


def _provider_market(internal_market: MarketCode | None) -> str | None:
    if internal_market is None:
        return None
    try:
        return _INTERNAL_TO_FINDB_MARKET[internal_market]
    except KeyError as error:
        raise UnsupportedMarketError(
            f"FinDB market mapping is not reviewed for internal market {internal_market!r}"
        ) from error


def _internal_market(provider_market: str) -> MarketCode:
    try:
        return _FINDB_TO_INTERNAL_MARKET[provider_market]
    except KeyError as error:
        raise UnsupportedMarketError(
            f"FinDB returned unmapped market code {provider_market!r}"
        ) from error


def _instrument_params(query: InstrumentQuery) -> dict[str, QueryValue]:
    params: dict[str, QueryValue] = {
        "include_count": query.include_count,
        "page": query.page,
        "page_size": query.page_size,
    }
    _add_optional(params, "market", _provider_market(query.market))
    _add_optional(params, "asset_class", query.asset_class)
    _add_optional(params, "status", query.status)
    _add_optional(params, "symbol", query.symbol)
    _add_optional(params, "cursor", query.cursor)
    return params


def _daily_bar_params(query: DailyBarQuery) -> dict[str, QueryValue]:
    params: dict[str, QueryValue] = {
        "page": query.page,
        "page_size": query.page_size,
    }
    _add_optional(params, "market", _provider_market(query.market))
    if query.symbols:
        params["symbols"] = ",".join(query.symbols)
    if query.start_date is not None:
        params["start_date"] = query.start_date.isoformat()
    if query.end_date is not None:
        params["end_date"] = query.end_date.isoformat()
    return params


def _add_optional(
    params: dict[str, QueryValue],
    name: str,
    value: QueryValue | None,
) -> None:
    if value is not None:
        params[name] = value


def _map_instrument(item: FinDBInstrument) -> Instrument:
    return Instrument(
        source_id=str(item.instrument_id),
        market=_internal_market(item.market),
        asset_class=item.asset_class,
        symbol=item.symbol,
        status=item.status,
        name=item.name,
        currency=item.currency,
        timezone=item.timezone,
        listed_date=item.listed_date,
        delisted_date=item.delisted_date,
        first_trade_date=item.first_trade_date,
        latest_trade_date=item.latest_trade_date,
        latest_price=item.latest_price,
    )


def _map_daily_bar(item: FinDBDailyBar) -> DailyBar:
    return DailyBar(
        instrument_source_id=str(item.instrument_id),
        market=_internal_market(item.market),
        symbol=item.symbol,
        trade_date=item.trade_date,
        name=item.name,
        open=item.open,
        high=item.high,
        low=item.low,
        close=item.close,
        volume=item.volume,
        total_ticks=item.total_ticks,
        turnover=item.turnover,
        source=item.source,
        source_created_at=item.created_at,
        source_updated_at=item.updated_at,
    )


def _page_info[RawT](page: FinDBPage[RawT]) -> PageInfo:
    return PageInfo(
        page=page.pagination.page,
        page_size=page.pagination.page_size,
        total_records=page.pagination.total_records,
        total_pages=page.pagination.total_pages,
        next_cursor=page.pagination.next_cursor,
    )


def _provenance(
    payload: FinDBTransportResponse,
    *,
    endpoint: str,
    params: Mapping[str, QueryValue],
    as_of: date | None,
    record_count: int,
) -> Provenance:
    canonical_query = json.dumps(
        sorted(params.items()),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return Provenance(
        contract_version=FINDB_CONTRACT_VERSION,
        contract_hash=FINDB_CONTRACT_HASH,
        endpoint=endpoint,
        query_fingerprint=hashlib.sha256(canonical_query).hexdigest(),
        fetched_at=payload.fetched_at,
        as_of=as_of,
        response_digest=payload.response_digest,
        record_count=record_count,
        request_id=payload.request_id,
    )


def _validate_max_pages(max_pages: int) -> None:
    if not 1 <= max_pages <= 10_000:
        raise ValueError("max_pages must be between 1 and 10000")
