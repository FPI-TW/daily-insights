from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars
import httpx
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.analyst_viewpoints.api import (
    AnalystViewpointClient,
    AnalystViewpointSyncError,
    record_sync_execution,
    sync_viewpoints,
)
from daily_insights_api.modules.assets.api import ObjectRef, ObjectStore, R2ObjectStore
from daily_insights_api.modules.data_sources.api import (
    TAIEX_CONTRACT_VERSION,
    TRACKED_INDICES,
    TWELVE_DATA_CONTRACT_VERSION,
    YFINANCE_CONTRACT_VERSION,
    DailyBar,
    DataSourceContractError,
    DataSourceTransientError,
    HeatmapImage,
    RetryPolicy,
    TwelveDataAdapter,
    TwelveDataTransport,
    TwseAdapter,
    YfinanceAdapter,
    YfinanceDailyBars,
    capture_sector_heatmap,
)
from daily_insights_api.modules.markets.api import (
    AUTOMATIC_SHORT_REFRESH_PERIOD,
    HEATMAP_MARKET_CODE,
    INSTITUTIONAL_MARKET_CODE,
    MAX_FETCH_CONCURRENCY,
    TAIEX_INCREMENTAL_MONTHS,
    TAIEX_SYMBOL,
    latest_index_bar_dates,
    select_index_refresh_period,
    select_taiex_refresh_months,
    store_index_daily_bars,
    store_institutional_market_flows,
    store_institutional_stock_flows,
)
from daily_insights_api.modules.orchestration.facts import (
    fence_is_current,
    interest_rate_month_coverage,
    latest_market_date,
    lock_sector_heatmap_rows,
    store_interest_rates,
    store_market_bars,
    store_sector_heatmap_images,
)
from daily_insights_api.modules.orchestration.models import (
    JobRun,
    MarketDailyObservation,
    MarketDailySeries,
)
from daily_insights_api.modules.orchestration.news_functions import build_news_handlers
from daily_insights_api.modules.orchestration.worker import (
    AttemptStatus,
    ClaimedFunction,
    FunctionHandler,
    FunctionOutcome,
)
from daily_insights_api.modules.reports.api import (
    FX_INSTRUMENTS,
    TENORS,
    diagnostics,
    load_sofr,
    load_treasury,
)

TwelveManifest = tuple[tuple[str, str, str, str | None, str | None], ...]
TREASURY_REQUEST_TIMEOUT_SECONDS = 30.0
HEATMAP_DATA_SOURCE = "HSI"
HONG_KONG = ZoneInfo("Asia/Hong_Kong")
# HKEX's trading calendar: holidays and half-day sessions, a year ahead. The
# heatmap is dated by it alone, so it never waits for a Yahoo bar and the
# morning window before the open is not spent waiting on another job.
# ponytail: built once per process with a one-year horizon; a worker that
# stays up past that horizon would date captures to the calendar's last day.
HK_CALENDAR = exchange_calendars.get_calendar("XHKG", start="2025-01-01")
# The heatmap moves once the pre-open auction starts and settles once the
# closing auction has (16:10); TradingView's HKEX feed is 15 minutes delayed
# (scanner ``update_mode`` is ``delayed_streaming_900``), so the auction's
# result reaches the page around 16:25.
HK_AUCTION_BEFORE_OPEN = pd.Timedelta(minutes=30)
HK_AUCTION_AFTER_CLOSE = pd.Timedelta(minutes=30)


def _utcnow() -> datetime:
    """Seam for tests; the heatmap gates read the clock before capturing."""
    return datetime.now(UTC)


def _market_open_outcome(*, manual: bool) -> FunctionOutcome:
    """Retrying would not help: the session lasts seven hours and the retry
    budget fifteen minutes. An operator pressing "refresh" during Taiwan
    office hours gets a skip they can read, the scheduled job a plain
    failure it should never reach (it runs at 08:00 Taipei)."""
    if manual:
        return FunctionOutcome(status="no_change", result={"skipped": "hk_market_open"})
    return FunctionOutcome(status="unavailable", error_code="hk_market_open")


class _LeaseLost(Exception):
    """Leaves the heatmap transaction by rollback: a return would commit it."""


def _function_run_lock(function_run_id: uuid.UUID) -> int:
    """Advisory lock key serialising the attempts of one function run."""
    return int.from_bytes(function_run_id.bytes[:8], "big", signed=True)


def hk_heatmap_session(captured_at: datetime) -> tuple[bool, date]:
    """(Hong Kong is trading, date of the last completed session) at that instant.

    Holidays and half-day sessions come from the exchange calendar, so the
    morning after a holiday dates a capture to the last session that traded,
    and a Christmas Eve capture at 12:30 is already a settled half day.
    """
    at = pd.Timestamp(captured_at)
    local_date = pd.Timestamp(captured_at.astimezone(HONG_KONG).date())
    trading = bool(HK_CALENDAR.is_session(local_date)) and (
        HK_CALENDAR.session_open(local_date) - HK_AUCTION_BEFORE_OPEN
        <= at
        < HK_CALENDAR.session_close(local_date) + HK_AUCTION_AFTER_CLOSE
    )
    session = HK_CALENDAR.date_to_session(local_date, direction="previous")
    if at < HK_CALENDAR.session_close(session) + HK_AUCTION_AFTER_CLOSE:
        session = HK_CALENDAR.previous_session(session)
    return trading, session.date()


def heatmap_object_key(data_source: str, edition_date: date, slug: str) -> str:
    return f"heatmaps/{data_source.lower()}/{edition_date.isoformat()}/{slug}.png"


def _treasury_fetch_periods(
    today: date, coverage: dict[tuple[int, int], set[str]]
) -> tuple[tuple[int, ...], tuple[tuple[int, int], ...]]:
    expected_symbols = {symbol for _, symbol in TENORS}
    years = {
        year
        for year in range(today.year - 2, today.year)
        if any(coverage.get((year, month), set()) != expected_symbols for month in range(1, 13))
    }
    current_year_is_partial = any(
        coverage.get((today.year, month), set()) != expected_symbols
        for month in range(1, today.month)
    )
    if current_year_is_partial or (not coverage and years):
        years.add(today.year)

    # Refetch from the month of the latest stored observation so month-end
    # rows published after the last run, and month starts that fall on a
    # weekend or holiday, still have a non-empty period to read.
    current = (today.year, today.month)
    period = min(max(coverage, default=current), current)
    months: set[tuple[int, int]] = set()
    while period <= current:
        if period[0] not in years:
            months.add(period)
        period = (period[0], period[1] + 1) if period[1] < 12 else (period[0] + 1, 1)
    return tuple(sorted(years)), tuple(sorted(months))


def _safe_error_detail(error: BaseException) -> str:
    """Bound provider errors before persisting them in admin-visible runs."""
    return f"{type(error).__name__}: {error}"[:500]


def _failure_detail(reasons: dict[str, str]) -> str | None:
    if not reasons:
        return None
    return "; ".join(f"{scope}={reason}" for scope, reason in reasons.items())[:500]


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


def _tracked_index_manifest(market_code: str) -> tuple[tuple[str, str, str], ...]:
    """The catalog's indices of one market, each fetched from Yahoo.

    Only the US, Hong Kong and China markets are passed in; ^TWII belongs to
    tw_equity and comes from the exchange, so it never appears here.
    """
    return tuple(
        (symbol, market, "index")
        for symbol, market in TRACKED_INDICES.items()
        if market == market_code
    )


YAHOO_MANIFESTS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "us_index_daily_bars": _tracked_index_manifest("us_equity"),
    "hk_index_daily_bars": _tracked_index_manifest("hk_equity"),
    "cn_index_daily_bars": _tracked_index_manifest("cn_equity"),
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
    object_store: ObjectStore | None = (
        R2ObjectStore.from_credentials(
            endpoint_url=settings.r2_endpoint_url,
            access_key_id=settings.r2_access_key_id.get_secret_value(),
            secret_access_key=settings.r2_secret_access_key.get_secret_value(),
        )
        if settings.r2_endpoint_url is not None
        and settings.r2_access_key_id is not None
        and settings.r2_secret_access_key is not None
        else None
    )

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

    async def tradingview(claimed: ClaimedFunction) -> FunctionOutcome:
        return await _run_tradingview_heatmap(settings, session_factory, claimed, object_store)

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
            "hsi_sector_heatmap": tradingview,
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
    failure_reasons: dict[str, str] = {}
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
        except Exception as error:
            failures.append(symbol)
            failure_reasons[symbol] = _safe_error_detail(error)
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
        result={
            "symbols": successes,
            "failed_symbols": failures,
            "failure_reasons": failure_reasons,
        },
        missing_scopes=tuple(failures),
        error_code="partial_symbols" if failures else None,
        error_detail=_failure_detail(failure_reasons),
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
    failure_reasons: dict[str, str] = {}
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
    # A symbol whose newest bar is less than a week old only needs the short
    # window; one with an older bar (worker or Yahoo was down) gets a window
    # that reaches back past it, and a new one (first deployment, or a symbol
    # just added to the catalog) gets the two-year bootstrap. Fetches run with
    # bounded concurrency so one slow
    # Yahoo answer does not push the whole attempt past its deadline; writes
    # stay sequential and in manifest order because the session is not safe
    # for concurrent use.
    latest_dates = await _latest_yahoo_bar_dates(
        session_factory,
        provider_key=claimed.provider_key,
        dataset_key=claimed.function_key,
        symbols=[symbol for symbol, _market, _unit in manifest],
    )
    semaphore = asyncio.Semaphore(MAX_FETCH_CONCURRENCY)

    async def fetch(symbol: str, market: str) -> YfinanceDailyBars | Exception:
        async with semaphore:
            try:
                return await adapter.get_daily_bars(
                    market=market,  # type: ignore[arg-type]
                    symbol=symbol,
                    period=select_index_refresh_period(
                        period=AUTOMATIC_SHORT_REFRESH_PERIOD,
                        latest_bar_date=latest_dates.get(symbol),
                        today=claimed.edition_date,
                    ),
                )
            except Exception as error:
                return error

    fetched = await asyncio.gather(*(fetch(symbol, market) for symbol, market, _unit in manifest))
    for (symbol, market, unit), result in zip(manifest, fetched, strict=True):
        try:
            if isinstance(result, Exception):
                raise result
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
        except Exception as error:
            failures.append(symbol)
            failure_reasons[symbol] = _safe_error_detail(error)
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
        result={
            "symbols": successes,
            "failed_symbols": failures,
            "failure_reasons": failure_reasons,
        },
        missing_scopes=tuple(failures),
        error_code="partial_symbols" if failures else None,
        error_detail=_failure_detail(failure_reasons),
        retryable=bool(failures),
    )


async def _latest_yahoo_bar_dates(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider_key: str,
    dataset_key: str,
    symbols: list[str],
) -> dict[str, date]:
    """symbol -> the date the next fetch must reach back past; absent = bootstrap.

    The worker fills two tables: the orchestration ledger that reports project
    from, and index_daily_bars that the index endpoints serve (tracked symbols
    only). Either one being behind must widen the window, so a tracked symbol
    reports the older of its two newest dates and is absent unless both tables
    hold it. That keeps this in step with the admin refresh, which sizes its
    window from index_daily_bars alone: re-pointing a symbol's provider deletes
    its rows there, and the next run of either path re-bootstraps it.
    """
    async with session_factory() as database:
        ledger = {
            symbol: latest
            for symbol, latest in (
                await database.execute(
                    select(
                        MarketDailySeries.symbol,
                        func.max(MarketDailyObservation.observation_date),
                    )
                    .join(
                        MarketDailyObservation,
                        MarketDailyObservation.series_id == MarketDailySeries.id,
                    )
                    .where(
                        MarketDailySeries.provider_key == provider_key,
                        MarketDailySeries.dataset_key == dataset_key,
                        MarketDailySeries.symbol.in_(symbols),
                    )
                    .group_by(MarketDailySeries.symbol)
                )
            ).tuples()
        }
        tracked = [symbol for symbol in symbols if symbol in TRACKED_INDICES]
        served = await latest_index_bar_dates(database, tracked)
        for symbol in tracked:
            if symbol in ledger and symbol in served:
                ledger[symbol] = min(ledger[symbol], served[symbol])
            else:
                ledger.pop(symbol, None)
        return ledger


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
        baseline_as_of = await latest_market_date(
            database,
            provider_key="twse",
            dataset_key=claimed.function_key,
            symbol=TAIEX_SYMBOL,
        )
        requested = list(
            await select_taiex_refresh_months(
                database,
                today=today,
                requested_months=TAIEX_INCREMENTAL_MONTHS,
            )
        )
    retry_scopes = set(claimed.scope.get("missing_scopes", ()))
    if retry_scopes:
        requested = [month for month in requested if month.isoformat() in retry_scopes]
    inserted = 0
    failed: list[str] = []
    failure_reasons: dict[str, str] = {}
    as_of = baseline_as_of
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
                month_as_of = bars[-1].trade_date
                as_of = month_as_of if as_of is None else max(as_of, month_as_of)
                fetched_at = result.fetched_at
        except Exception as error:
            scope = month.isoformat()
            failed.append(scope)
            failure_reasons[scope] = _safe_error_detail(error)
    outcome = _simple_outcome(inserted, as_of, fetched_at, failed)
    return FunctionOutcome(
        status=outcome.status,
        source_as_of=outcome.source_as_of,
        fetched_at=outcome.fetched_at,
        record_count=outcome.record_count,
        result={
            "failed_months": failed,
            "failure_reasons": failure_reasons,
        },
        missing_scopes=outcome.missing_scopes,
        error_code=outcome.error_code,
        error_detail=_failure_detail(failure_reasons),
        retryable=outcome.retryable,
    )


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
    symbols = tuple(symbol for _, symbol in TENORS)
    async with session_factory() as database:
        coverage = await interest_rate_month_coverage(
            database,
            provider_key="us_treasury",
            dataset_key=claimed.function_key,
            symbols=symbols,
            start_year=today.year - 2,
            end_year=today.year,
        )
    fetch_years, fetch_months = _treasury_fetch_periods(today, coverage)
    diagnostic_entries: list[Any] = []
    token = diagnostics.set(diagnostic_entries)
    try:
        async with httpx.AsyncClient(
            timeout=TREASURY_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            histories = await load_treasury(
                client,
                today,
                years=fetch_years,
                months=fetch_months,
            )
    finally:
        diagnostics.reset(token)
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
    failure_reasons = {
        item: failure.failure_type
        for source, failure in diagnostic_entries
        if source == "us_treasury"
        for item in failure.affected_items
    }
    failed_scopes = tuple(dict.fromkeys((*missing, *failure_reasons)))
    status: AttemptStatus = (
        "partial"
        if successes and failed_scopes
        else "unavailable"
        if failed_scopes
        else "succeeded"
        if inserted
        else "no_change"
    )
    return FunctionOutcome(
        status=status,
        source_as_of=as_of,
        fetched_at=datetime.now(UTC),
        record_count=inserted,
        result={
            "symbols": successes,
            "fetched_periods": [
                *[str(year) for year in fetch_years],
                *[f"{year:04d}-{month:02d}" for year, month in fetch_months],
            ],
            "failed_symbols": missing,
            "failure_reasons": failure_reasons,
        },
        missing_scopes=failed_scopes,
        error_code="partial_scopes" if failed_scopes else None,
        error_detail=_failure_detail(failure_reasons),
        retryable=bool(failed_scopes),
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


async def _run_tradingview_heatmap(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
    object_store: ObjectStore | None,
) -> FunctionOutcome:
    if not settings.tradingview_enabled:
        return FunctionOutcome(status="unavailable", error_code="tradingview_disabled")
    if object_store is None or settings.r2_bucket_name is None:
        return FunctionOutcome(status="unavailable", error_code="r2_not_configured")
    # Cheap exits before the headless browser session: a lost lease or a
    # missing ^HSI series would only throw the capture away afterwards. Both
    # are read again under the lock below, where they are authoritative.
    async with session_factory() as database:
        if not await fence_is_current(
            database,
            function_run_id=claimed.function_run_id,
            fence_token=claimed.fence_token,
            for_update=False,
        ):
            return FunctionOutcome(status="cancelled", error_code="cancelled")
        job_run = await database.get(JobRun, claimed.job_run_id)
        manual = job_run is not None and job_run.trigger == "manual"
    # The session gate also runs here, on the clock, so a run during the
    # session does not pay for a browser session it would throw away. It runs
    # again on the capture's own timestamp below, which is the one that counts.
    if hk_heatmap_session(_utcnow())[0]:
        return _market_open_outcome(manual=manual)
    try:
        captured = await capture_sector_heatmap(
            data_source=HEATMAP_DATA_SOURCE,
            timeout_seconds=settings.tradingview_timeout_seconds,
        )
    except DataSourceTransientError as error:
        return FunctionOutcome(
            status="unavailable",
            error_code="heatmap_capture_failed",
            error_detail=_safe_error_detail(error),
            retryable=True,
        )
    except DataSourceContractError as error:
        return FunctionOutcome(
            status="unavailable",
            error_code="heatmap_contract_drift",
            error_detail=_safe_error_detail(error),
        )
    # A capture taken while Hong Kong trades shows an unfinished session. There
    # is no settled bar to date it by, and dating it to the previous bar would
    # overwrite that day's snapshot in place, so it is not stored at all.
    # The authoritative gate: the capture's own timestamp, taken when the
    # pixels were, so a run that started just before the open is still caught.
    # Outside the session the capture shows the last completed session, and
    # that is the edition it is filed under.
    trading, edition = hk_heatmap_session(captured.captured_at)
    if trading:
        return _market_open_outcome(manual=manual)
    bucket = settings.r2_bucket_name
    uploaded = 0
    try:
        async with session_factory.begin() as database:
            # Attempts of this function run take turns through an advisory lock,
            # not a lock on the function_runs row: the row stays free for the
            # worker's heartbeat while the R2 I/O below runs. A stale attempt that
            # gets through the plain fence read here is caught by the locking
            # fence check right before commit, and because turns do not overlap
            # the newer attempt's uploads always land after the stale one's.
            await database.execute(
                select(func.pg_advisory_xact_lock(_function_run_lock(claimed.function_run_id)))
            )
            if not await fence_is_current(
                database,
                function_run_id=claimed.function_run_id,
                fence_token=claimed.fence_token,
                for_update=False,
            ):
                return FunctionOutcome(status="cancelled", error_code="cancelled")
            existing = await lock_sector_heatmap_rows(
                database, data_source=HEATMAP_DATA_SOURCE, edition_date=edition
            )
            keyed: list[tuple[HeatmapImage, str]] = []
            for image in captured.images:
                key = heatmap_object_key(HEATMAP_DATA_SOURCE, edition, image.slug)
                keyed.append((image, key))
                ref = ObjectRef(bucket, key)
                row = existing.get(image.slug)
                if row is not None and row.sha256 == image.sha256:
                    # Unchanged on the ledger, but a stale attempt may have left
                    # its own bytes under this key before it was rolled back, so
                    # the bucket has the last word on whether to upload again.
                    head = await object_store.head(ref)
                    if head is not None and head.sha256 == image.sha256:
                        continue
                # A rerun on the same day replaces the day's objects in place.
                await object_store.overwrite(
                    ref,
                    io.BytesIO(image.png),
                    size_bytes=len(image.png),
                    mime_type="image/png",
                    sha256=image.sha256,
                )
                uploaded += 1
            stored = await store_sector_heatmap_images(
                database,
                function_attempt_id=claimed.attempt_id,
                data_source=HEATMAP_DATA_SOURCE,
                market_code=HEATMAP_MARKET_CODE,
                edition_date=edition,
                captured_at=captured.captured_at,
                images=keyed,
                existing=existing,
                retention_days=settings.tradingview_heatmap_retention_days,
            )
            # Delete the objects before the row deletions commit. A delete that
            # fails rolls the rows back, so the next run finds them and retries;
            # committing first would drop the only record of the key and orphan
            # the object for good. R2 deletes are idempotent, so a commit that
            # fails after the objects are gone just repeats them next run.
            for key in stored.removed_object_keys:
                await object_store.delete(ObjectRef(bucket, key))
            # The last word before commit, with the row locked and no I/O after
            # it. Leaving the `begin()` block normally commits, so a stale attempt
            # must leave it by exception for its rows and deletions to roll back.
            if not await fence_is_current(
                database,
                function_run_id=claimed.function_run_id,
                fence_token=claimed.fence_token,
            ):
                raise _LeaseLost
    except _LeaseLost:
        return FunctionOutcome(status="cancelled", error_code="cancelled")
    # A retention sweep that only deleted objects still changed the bucket.
    return FunctionOutcome(
        status="succeeded" if stored.changed or stored.removed_object_keys else "no_change",
        source_as_of=edition,
        fetched_at=captured.captured_at,
        record_count=stored.changed,
        result={
            "images": len(captured.images),
            "uploaded": uploaded,
            "removed_objects": len(stored.removed_object_keys),
        },
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
