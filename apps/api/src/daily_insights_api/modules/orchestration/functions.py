from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.analyst_viewpoints.api import (
    AnalystViewpointClient,
    AnalystViewpointSyncError,
    record_sync_execution,
    sync_viewpoints,
)
from daily_insights_api.modules.data_sources.api import (
    TAIEX_CONTRACT_VERSION,
    TRACKED_INDICES,
    TWELVE_DATA_CONTRACT_VERSION,
    YFINANCE_CONTRACT_VERSION,
    DailyBar,
    RetryPolicy,
    TwelveDataAdapter,
    TwelveDataTransport,
    TwseAdapter,
    YfinanceAdapter,
)
from daily_insights_api.modules.markets.api import (
    INSTITUTIONAL_MARKET_CODE,
    TAIEX_SYMBOL,
    store_index_daily_bars,
    store_institutional_market_flows,
    store_institutional_stock_flows,
)
from daily_insights_api.modules.orchestration.facts import (
    fence_is_current,
    latest_market_date,
    store_interest_rates,
    store_market_bars,
)
from daily_insights_api.modules.orchestration.models import JobRun
from daily_insights_api.modules.orchestration.news_functions import build_news_handlers
from daily_insights_api.modules.orchestration.worker import (
    AttemptStatus,
    ClaimedFunction,
    FunctionHandler,
    FunctionOutcome,
)
from daily_insights_api.modules.reports.api import (
    FX_INSTRUMENTS,
    load_sofr,
    load_treasury,
)

TwelveManifest = tuple[tuple[str, str, str, str | None, str | None], ...]

TWELVE_MANIFESTS: dict[str, TwelveManifest] = {
    "commodity_daily_bars": (
        ("WTI/USD", "global_macro_bonds", "USD", "commodity", "Energy Resource"),
        ("XBR/USD", "global_macro_bonds", "USD", "commodity", "Energy Resource"),
        ("XAU/USD", "global_macro_bonds", "USD", "commodity", "Precious Metal"),
        ("XAG/USD", "global_macro_bonds", "USD", "commodity", "Precious Metal"),
        ("HG1", "global_macro_bonds", "USD", "commodity", "Industrial Metal"),
    ),
    "fx_daily_bars": tuple(
        (symbol, "forex", currency, None, "Physical Currency")
        for _, symbol, currency in FX_INSTRUMENTS
    ),
    "rates_proxy_daily_bars": (
        ("TLT", "global_macro_bonds", "USD", None, None),
        ("IEF", "global_macro_bonds", "USD", None, None),
        ("UUP", "global_macro_bonds", "USD", None, None),
    ),
    "crypto_daily_bars": tuple(
        (symbol, "crypto", "USD", None, None)
        for symbol in ("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD")
    ),
    "us_mega_cap_daily_bars": tuple(
        (symbol, "us_equity", "USD", None, None)
        for symbol in ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA")
    ),
}

TWELVE_BOOTSTRAP = {
    "commodity_daily_bars": 800,
    "fx_daily_bars": 400,
    "rates_proxy_daily_bars": 400,
    "crypto_daily_bars": 485,
    "us_mega_cap_daily_bars": 400,
}

YAHOO_MANIFESTS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "us_index_daily_bars": tuple(
        (symbol, "us_equity", "index")
        for symbol in ("^DJI", "^GSPC", "^NDX", "^RUT", "^SOX", "^VIX")
    ),
    "hk_index_daily_bars": (("^HSI", "hk_equity", "index"),),
    "cn_index_daily_bars": (("000001.SS", "cn_equity", "index"),),
    "dxy_daily_bars": (("DX-Y.NYB", "forex", "index"),),
}


def build_function_handlers(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> FunctionHandlers:
    twelve_transport = (
        TwelveDataTransport(
            base_url=settings.twelve_data_base_url,
            api_key=settings.twelve_data_api_key,
            timeout_seconds=settings.twelve_data_timeout_seconds,
            retry_policy=RetryPolicy(max_attempts=1),
            max_concurrency=settings.twelve_data_max_concurrency,
        )
        if settings.twelve_data_api_key is not None
        else None
    )
    twelve_adapter = TwelveDataAdapter(twelve_transport) if twelve_transport is not None else None
    yahoo_adapter = YfinanceAdapter(timeout_seconds=settings.yfinance_timeout_seconds)
    twse_adapter = _twse_adapter(settings)

    async def twelve(claimed: ClaimedFunction) -> FunctionOutcome:
        return await _run_twelve(settings, session_factory, claimed, twelve_adapter)

    async def yahoo(claimed: ClaimedFunction) -> FunctionOutcome:
        return await _run_yahoo(settings, session_factory, claimed, yahoo_adapter)

    async def twse_taiex(claimed: ClaimedFunction) -> FunctionOutcome:
        return await _run_twse_taiex(settings, session_factory, claimed, twse_adapter)

    async def twse_stock(claimed: ClaimedFunction) -> FunctionOutcome:
        return await _run_twse_stock_flows(settings, session_factory, claimed, twse_adapter)

    async def twse_market(claimed: ClaimedFunction) -> FunctionOutcome:
        return await _run_twse_market_flows(settings, session_factory, claimed, twse_adapter)

    handlers: dict[str, FunctionHandler] = {}
    for key in TWELVE_MANIFESTS:
        handlers[key] = twelve
    for key in YAHOO_MANIFESTS:
        handlers[key] = yahoo
    handlers.update(
        {
            "taiex_daily_bars": twse_taiex,
            "institutional_stock_flows": twse_stock,
            "institutional_market_flows": twse_market,
            "treasury_yield_curve": _bind(_run_treasury, settings, session_factory),
            "sofr_daily_rates": _bind(_run_sofr, settings, session_factory),
            "analyst_viewpoints_sync": _bind(_run_analyst, settings, session_factory),
        }
    )
    handlers.update(build_news_handlers(settings, session_factory))
    return FunctionHandlers(handlers, twelve_transport=twelve_transport, twse_adapter=twse_adapter)


class FunctionHandlers(dict[str, FunctionHandler]):
    def __init__(
        self,
        handlers: dict[str, FunctionHandler],
        *,
        twelve_transport: TwelveDataTransport | None,
        twse_adapter: TwseAdapter,
    ) -> None:
        super().__init__(handlers)
        self._twelve_transport = twelve_transport
        self._twse_adapter = twse_adapter

    async def close(self) -> None:
        if self._twelve_transport is not None:
            await self._twelve_transport.close()
        await self._twse_adapter.close()


def _bind(
    handler: Callable[
        [Settings, async_sessionmaker[AsyncSession], ClaimedFunction],
        Awaitable[FunctionOutcome],
    ],
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> FunctionHandler:
    async def bound(claimed: ClaimedFunction) -> FunctionOutcome:
        return await handler(settings, session_factory, claimed)

    return bound


def _request_metadata(provenance: Any) -> dict[str, Any]:
    return {
        "provider": provenance.provider,
        "endpoint": provenance.endpoint,
        "query_fingerprint": provenance.query_fingerprint,
        "response_digest": provenance.response_digest,
        "request_id": provenance.request_id,
        "fetched_at": provenance.fetched_at.isoformat(),
        "source_as_of": provenance.as_of.isoformat() if provenance.as_of else None,
        "record_count": provenance.record_count,
    }


async def _twelve_outputsize(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    function_key: str,
    symbol: str,
    provider_key: str,
    edition_date: date,
) -> int:
    async with session_factory() as database:
        latest = await latest_market_date(
            database,
            provider_key=provider_key,
            dataset_key=function_key,
            symbol=symbol,
        )
    if latest is None:
        return TWELVE_BOOTSTRAP[function_key]
    missing_calendar_days = max(0, (edition_date - latest).days)
    return min(
        TWELVE_BOOTSTRAP[function_key],
        max(2, missing_calendar_days * 2 + 10),
    )


async def _run_twelve(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
    adapter: TwelveDataAdapter | None,
) -> FunctionOutcome:
    if settings.twelve_data_api_key is None or adapter is None:
        return FunctionOutcome(
            status="unavailable",
            error_code="twelve_data_disabled",
            retryable=False,
        )
    retry_scopes = set(claimed.scope.get("missing_scopes", ()))
    manifest = tuple(
        item
        for item in TWELVE_MANIFESTS[claimed.function_key]
        if not retry_scopes or item[0] in retry_scopes
    )
    successes: list[str] = []
    failures: list[str] = []
    metadata: list[dict[str, Any]] = []
    inserted = 0
    as_of: date | None = None
    fetched_at: datetime | None = None
    for symbol, market, unit, symbol_type, asset_type in manifest:
        try:
            outputsize = await _twelve_outputsize(
                session_factory,
                function_key=claimed.function_key,
                symbol=symbol,
                provider_key=claimed.provider_key,
                edition_date=claimed.edition_date,
            )
            result = await adapter.get_completed_prices(
                market=market,  # type: ignore[arg-type]
                symbols=(symbol,),
                expected_currencies={symbol: unit},
                symbol_types={symbol: symbol_type} if symbol_type else None,
                expected_asset_types={symbol: asset_type} if asset_type else None,
                outputsize=outputsize,
            )
            item = result.items[0]
            async with session_factory.begin() as database:
                inserted += await store_market_bars(
                    database,
                    function_run_id=claimed.function_run_id,
                    fence_token=claimed.fence_token,
                    function_attempt_id=claimed.attempt_id,
                    provider_key=claimed.provider_key,
                    dataset_key=claimed.function_key,
                    symbol=symbol,
                    market=market,
                    unit=unit,
                    contract_version=TWELVE_DATA_CONTRACT_VERSION,
                    bars=item.bars,
                )
            successes.append(symbol)
            as_of = item.as_of if as_of is None else min(as_of, item.as_of)
            metadata.extend(_request_metadata(value) for value in result.provenances)
            fetched_at = max(value.fetched_at for value in result.provenances)
        except Exception:
            failures.append(symbol)
    status: AttemptStatus = (
        "partial"
        if successes and failures
        else "unavailable"
        if failures
        else "succeeded"
        if inserted
        else "no_change"
    )
    digest = _metadata_digest(metadata) if metadata else None
    return FunctionOutcome(
        status=status,
        source_as_of=as_of,
        fetched_at=fetched_at,
        record_count=inserted,
        payload_digest=digest,
        request_metadata=tuple(metadata),
        result={"symbols": successes, "failed_symbols": failures},
        missing_scopes=tuple(failures),
        error_code="partial_symbols" if failures else None,
        retryable=bool(failures),
    )


async def _run_yahoo(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
    adapter: YfinanceAdapter,
) -> FunctionOutcome:
    if not settings.yfinance_enabled:
        return FunctionOutcome(status="unavailable", error_code="yfinance_disabled")
    successes: list[str] = []
    failures: list[str] = []
    metadata: list[dict[str, Any]] = []
    inserted = 0
    as_of: date | None = None
    fetched_at: datetime | None = None
    retry_scopes = set(claimed.scope.get("missing_scopes", ()))
    manifest = tuple(
        item
        for item in YAHOO_MANIFESTS[claimed.function_key]
        if not retry_scopes or item[0] in retry_scopes
    )
    for symbol, market, unit in manifest:
        try:
            result = await adapter.get_daily_bars(
                market=market,  # type: ignore[arg-type]
                symbol=symbol,
                period="2y",
            )
            async with session_factory.begin() as database:
                inserted += await store_market_bars(
                    database,
                    function_run_id=claimed.function_run_id,
                    fence_token=claimed.fence_token,
                    function_attempt_id=claimed.attempt_id,
                    provider_key=claimed.provider_key,
                    dataset_key=claimed.function_key,
                    symbol=symbol,
                    market=market,
                    unit=unit,
                    contract_version=YFINANCE_CONTRACT_VERSION,
                    bars=result.items,
                )
                if symbol in TRACKED_INDICES and await fence_is_current(
                    database,
                    function_run_id=claimed.function_run_id,
                    fence_token=claimed.fence_token,
                ):
                    await store_index_daily_bars(
                        database,
                        bars=result.items,
                        provider="yfinance",
                        contract_version=YFINANCE_CONTRACT_VERSION,
                        source_fetched_at=result.provenance.fetched_at,
                    )
            successes.append(symbol)
            source_date = result.items[-1].trade_date
            as_of = source_date if as_of is None else min(as_of, source_date)
            fetched_at = result.provenance.fetched_at
            metadata.append(_request_metadata(result.provenance))
        except Exception:
            failures.append(symbol)
    status: AttemptStatus = (
        "partial"
        if successes and failures
        else "unavailable"
        if failures
        else "succeeded"
        if inserted
        else "no_change"
    )
    return FunctionOutcome(
        status=status,
        source_as_of=as_of,
        fetched_at=fetched_at,
        record_count=inserted,
        payload_digest=_metadata_digest(metadata) if metadata else None,
        request_metadata=tuple(metadata),
        result={"symbols": successes, "failed_symbols": failures},
        missing_scopes=tuple(failures),
        error_code="partial_symbols" if failures else None,
        retryable=bool(failures),
    )


def _twse_adapter(settings: Settings) -> TwseAdapter:
    return TwseAdapter(
        base_url=settings.twse_base_url,
        timeout_seconds=settings.twse_timeout_seconds,
        request_interval_seconds=settings.twse_request_interval_seconds,
        max_attempts=1,
    )


async def _run_twse_taiex(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
    adapter: TwseAdapter,
) -> FunctionOutcome:
    if not settings.twse_enabled:
        return FunctionOutcome(status="unavailable", error_code="twse_disabled")
    today = claimed.edition_date
    async with session_factory() as database:
        latest = await latest_market_date(
            database,
            provider_key="twse",
            dataset_key=claimed.function_key,
            symbol=TAIEX_SYMBOL,
        )
    months = 25 if latest is None else 2
    first = today.replace(day=1)
    requested: list[date] = []
    cursor = first
    for _ in range(months):
        requested.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    requested.reverse()
    retry_scopes = set(claimed.scope.get("missing_scopes", ()))
    if retry_scopes:
        requested = [month for month in requested if month.isoformat() in retry_scopes]
    inserted = 0
    failed: list[str] = []
    as_of: date | None = None
    fetched_at: datetime | None = None
    for month in requested:
        try:
            result = await adapter.get_taiex_daily_bars(month)
            bars = tuple(
                DailyBar(
                    instrument_source_id=TAIEX_SYMBOL,
                    market="tw_equity",
                    symbol=TAIEX_SYMBOL,
                    trade_date=item.trade_date,
                    open=item.open,
                    high=item.high,
                    low=item.low,
                    close=item.close,
                    volume=item.volume,
                    trade_value=item.trade_value,
                    source="twse",
                )
                for item in result.items
            )
            async with session_factory.begin() as database:
                inserted += await store_market_bars(
                    database,
                    function_run_id=claimed.function_run_id,
                    fence_token=claimed.fence_token,
                    function_attempt_id=claimed.attempt_id,
                    provider_key="twse",
                    dataset_key=claimed.function_key,
                    symbol=TAIEX_SYMBOL,
                    market="tw_equity",
                    unit="index",
                    contract_version=TAIEX_CONTRACT_VERSION,
                    bars=bars,
                    source_timestamp=result.fetched_at,
                )
                if await fence_is_current(
                    database,
                    function_run_id=claimed.function_run_id,
                    fence_token=claimed.fence_token,
                ):
                    await store_index_daily_bars(
                        database,
                        bars=bars,
                        provider="twse",
                        contract_version=TAIEX_CONTRACT_VERSION,
                        source_fetched_at=result.fetched_at,
                        preserve_existing_activity=True,
                    )
            if bars:
                as_of = bars[-1].trade_date
                fetched_at = result.fetched_at
        except Exception:
            failed.append(month.isoformat())
    return _simple_outcome(inserted, as_of, fetched_at, failed)


async def _latest_twse_result(
    adapter: TwseAdapter,
    *,
    stock: bool,
    edition_date: date,
) -> Any:
    for offset in range(10):
        day = edition_date - timedelta(days=offset)
        result = (
            await adapter.get_stock_flows(day) if stock else await adapter.get_market_flows(day)
        )
        if result.items:
            return result
    return result


async def _run_twse_stock_flows(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
    adapter: TwseAdapter,
) -> FunctionOutcome:
    if not settings.twse_enabled:
        return FunctionOutcome(status="unavailable", error_code="twse_disabled")
    result = await _latest_twse_result(adapter, stock=True, edition_date=claimed.edition_date)
    if not result.items:
        return FunctionOutcome(status="no_change", source_as_of=result.trade_date, record_count=0)
    async with session_factory.begin() as database:
        if not await fence_is_current(
            database,
            function_run_id=claimed.function_run_id,
            fence_token=claimed.fence_token,
        ):
            return FunctionOutcome(status="cancelled", error_code="cancelled")
        count = await store_institutional_stock_flows(
            database, market_code=INSTITUTIONAL_MARKET_CODE, flows=result
        )
    return FunctionOutcome(
        status="succeeded",
        source_as_of=result.trade_date,
        fetched_at=result.fetched_at,
        record_count=count,
        result={"trade_date": result.trade_date.isoformat()},
    )


async def _run_twse_market_flows(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
    adapter: TwseAdapter,
) -> FunctionOutcome:
    if not settings.twse_enabled:
        return FunctionOutcome(status="unavailable", error_code="twse_disabled")
    result = await _latest_twse_result(adapter, stock=False, edition_date=claimed.edition_date)
    if not result.items:
        return FunctionOutcome(status="no_change", source_as_of=result.trade_date, record_count=0)
    async with session_factory.begin() as database:
        if not await fence_is_current(
            database,
            function_run_id=claimed.function_run_id,
            fence_token=claimed.fence_token,
        ):
            return FunctionOutcome(status="cancelled", error_code="cancelled")
        count = await store_institutional_market_flows(
            database, market_code=INSTITUTIONAL_MARKET_CODE, flows=result
        )
    return FunctionOutcome(
        status="succeeded",
        source_as_of=result.trade_date,
        fetched_at=result.fetched_at,
        record_count=count,
        result={"trade_date": result.trade_date.isoformat()},
    )


async def _run_treasury(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
) -> FunctionOutcome:
    del settings
    today = claimed.edition_date
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        histories = await load_treasury(client, today)
    inserted = 0
    missing: list[str] = []
    successes: list[str] = []
    as_of: date | None = None
    async with session_factory.begin() as database:
        for history in histories:
            if not history.points:
                missing.append(history.symbol)
                continue
            successes.append(history.symbol)
            inserted += await store_interest_rates(
                database,
                function_run_id=claimed.function_run_id,
                fence_token=claimed.fence_token,
                function_attempt_id=claimed.attempt_id,
                provider_key="us_treasury",
                dataset_key=claimed.function_key,
                symbol=history.symbol,
                market="global_macro_bonds",
                unit="percent",
                contract_version="us-treasury-yield-curve.v1",
                values=tuple((point.date, point.value) for point in history.points),
            )
            source_date = history.points[-1].date
            as_of = source_date if as_of is None else min(as_of, source_date)
    status: AttemptStatus = (
        "partial"
        if successes and missing
        else "unavailable"
        if missing
        else "succeeded"
        if inserted
        else "no_change"
    )
    return FunctionOutcome(
        status=status,
        source_as_of=as_of,
        fetched_at=datetime.now(UTC),
        record_count=inserted,
        result={"symbols": successes, "failed_symbols": missing},
        missing_scopes=tuple(missing),
        error_code="partial_scopes" if missing else None,
        retryable=bool(missing),
    )


async def _run_sofr(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
) -> FunctionOutcome:
    del settings
    today = claimed.edition_date
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        history = await load_sofr(client, today)
    if not history.points:
        return FunctionOutcome(status="unavailable", error_code="empty_sofr", retryable=True)
    async with session_factory.begin() as database:
        inserted = await store_interest_rates(
            database,
            function_run_id=claimed.function_run_id,
            fence_token=claimed.fence_token,
            function_attempt_id=claimed.attempt_id,
            provider_key="new_york_fed",
            dataset_key=claimed.function_key,
            symbol=history.symbol,
            market="global_macro_bonds",
            unit="percent",
            contract_version="new-york-fed-sofr.v1",
            values=tuple((point.date, point.value) for point in history.points),
        )
    return FunctionOutcome(
        status="succeeded" if inserted else "no_change",
        source_as_of=history.points[-1].date,
        fetched_at=datetime.now(UTC),
        record_count=inserted,
    )


async def _run_analyst(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
) -> FunctionOutcome:
    if not settings.analyst_viewpoints_enabled or settings.analyst_viewpoints_api_key is None:
        return FunctionOutcome(status="unavailable", error_code="analyst_viewpoints_disabled")
    client = AnalystViewpointClient(
        base_url=settings.analyst_viewpoints_base_url,
        api_key=settings.analyst_viewpoints_api_key,
        timeout_seconds=settings.analyst_viewpoints_timeout_seconds,
    )
    async with session_factory() as database:
        job_run = await database.get(JobRun, claimed.job_run_id)
        if job_run is None:
            raise ValueError("job run not found")
        trigger = "manual" if job_run.trigger == "manual" else "scheduler"
    try:
        async with session_factory.begin() as database:
            result = await sync_viewpoints(
                database,
                client,
                claimed.edition_date,
                function_attempt_id=claimed.attempt_id,
                fence_token=claimed.fence_token,
            )
            await record_sync_execution(
                database,
                viewpoint_date=result.viewpoint_date,
                trigger=trigger,
                result=result,
                function_attempt_id=claimed.attempt_id,
            )
    except Exception as error:
        error_code = (
            error.code
            if isinstance(error, AnalystViewpointSyncError)
            else type(error).__name__[:100]
        )
        try:
            async with session_factory.begin() as database:
                await record_sync_execution(
                    database,
                    viewpoint_date=claimed.edition_date,
                    trigger=trigger,
                    error_code=error_code,
                    function_attempt_id=claimed.attempt_id,
                )
        except Exception:
            pass
        raise
    missing = tuple(item.market_code for item in result.markets if item.status != "updated")
    return FunctionOutcome(
        status="partial" if missing else "succeeded",
        source_as_of=result.viewpoint_date,
        fetched_at=result.fetched_at,
        record_count=sum(item.status == "updated" for item in result.markets),
        missing_scopes=missing,
        result=result.model_dump(mode="json"),
        retryable=bool(missing),
    )


def _simple_outcome(
    inserted: int,
    as_of: date | None,
    fetched_at: datetime | None,
    missing: list[str],
) -> FunctionOutcome:
    status: AttemptStatus = (
        "partial"
        if inserted and missing
        else "unavailable"
        if missing and not inserted
        else "succeeded"
        if inserted
        else "no_change"
    )
    return FunctionOutcome(
        status=status,
        source_as_of=as_of,
        fetched_at=fetched_at,
        record_count=inserted,
        missing_scopes=tuple(missing),
        error_code="partial_scopes" if missing else None,
        retryable=bool(missing),
    )


def _metadata_digest(metadata: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        "|".join(sorted(str(item["response_digest"]) for item in metadata)).encode()
    ).hexdigest()
