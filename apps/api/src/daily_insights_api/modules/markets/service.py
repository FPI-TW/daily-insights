import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import Result, String, column, func, literal_column, select, true, update, values
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from daily_insights_api.modules.data_sources.api import (
    TAIEX_CONTRACT_VERSION,
    TRACKED_INDICES,
    DailyBar,
    DataSourceContractError,
    DataSourceError,
    IndexSymbol,
    MarketCode,
    TwseAdapter,
    TwseMarketFlows,
    TwseStockFlows,
    YfinanceAdapter,
    YfinanceDailyBars,
)
from daily_insights_api.modules.markets.catalog import MARKETS
from daily_insights_api.modules.markets.models import (
    INVESTOR_TYPES,
    IndexDailyBar,
    IndexDailyBarSeries,
    InstitutionalMarketFlow,
    InstitutionalStockFlow,
    Market,
    OrganizationMarketPolicy,
)
from daily_insights_api.modules.markets.schemas import (
    IndexDailyBarResponse,
    IndexLatestBarResponse,
    IndexMovingAverage20SeriesResponse,
    IndexMovingAverage60SeriesResponse,
    IndexMovingAverage120SeriesResponse,
    IndexMovingAverage240SeriesResponse,
    IndexMovingAveragePointResponse,
    IndexMovingAveragesResponse,
    InstitutionalFlowPointResponse,
    InstitutionalStockFlowResponse,
    MarketResponse,
)

MAX_BIND_PARAMETERS = 65535
# Yahoo publishes no rate limit and is reached through a scraping client, so
# this stays conservative; it matches TwelveDataTransport's default.
MAX_FETCH_CONCURRENCY = 4
INITIAL_SERIES_BACKFILL_PERIOD = "2y"
# Deliberately a week, not a day. A one-day window holds nothing but the most
# recent session, and that session is regularly unusable: mid-morning in Taipei
# it is the still-open local one, and Yahoo publishes the just-closed Asian
# session with open/high/low but a NaN close for hours. Either way the adapter
# drops the only row it was given and the symbol fails. A week-wide window
# always carries settled days behind whatever the newest row is doing, and
# absorbs holidays and a missed run without any catch-up logic.
AUTOMATIC_SHORT_REFRESH_PERIOD = "7d"
YAHOO_REFRESH_LOCK_KEY = 4_741_901_938_764_211_037
# ^TWII comes from the exchange itself rather than Yahoo. Two TWSE reports hold
# one bar between them -- MI_5MINS_HIST has open/high/low/close, FMTQIK has the
# volume -- and both answer a whole month per request. Yahoo's ^TWII volume did
# not agree with the exchange's published share count, so this is the only
# source for the series; `index_daily_bar_series` enforces that one provider
# owns it.
TAIEX_SYMBOL: IndexSymbol = "^TWII"
TAIEX_PROVIDER = "twse"
# The Yahoo path gives a series with no stored bars a two-year bootstrap so a
# catalog rollout needs no manual backfill. TWSE is keyed on months rather than
# a lookback window, so this is the same two years counted its way: 24 months
# back plus the current one. Without it a fresh series would only ever hold the
# incremental window, and nothing would go back for the rest.
TAIEX_INCREMENTAL_MONTHS = 2
TAIEX_INITIAL_BACKFILL_MONTHS = 25
# TWSE being down looks the same for every month, so stop asking after three.
# Mirrors the institutional-flows walk, which paces the same provider.
MAX_CONSECUTIVE_TAIEX_FAILURES = 3
# Deliberately NOT the Yahoo key. That lock exists to stop two Yahoo callers
# from duplicating credits and cookies for a provider with no published quota;
# TWSE is a different provider that paces itself inside TwseAdapter. Sharing
# the key made a 25-month backfill -- minutes of spaced requests -- block the
# admin page's manual Yahoo refresh past its 45s deadline. This one is held
# per transaction, so it covers the write and nothing else.
TAIEX_REFRESH_LOCK_KEY = 7_215_884_310_662_047_913
# Every other tracked index still comes from Yahoo.
YFINANCE_INDICES: tuple[IndexSymbol, ...] = tuple(
    symbol for symbol in TRACKED_INDICES if symbol != TAIEX_SYMBOL
)
MOVING_AVERAGE_PERIODS = (20, 60, 120, 240)
MOVING_AVERAGE_WARMUP_SESSIONS = max(MOVING_AVERAGE_PERIODS) - 1
MOVING_AVERAGE_QUANTUM = Decimal("0.0000000001")


class IndexProviderConflictError(ValueError):
    """A symbol already has durable bars owned by another provider."""


def select_index_refresh_period(*, period: str, has_stored_bars: bool) -> str:
    """Choose the fetch window without changing explicit long-period requests.

    The automatic/admin short refresh uses ``7d``.  A missing series needs the
    normal two-year bootstrap for YTD calculations, while every other request
    remains exactly as requested.
    """
    if not has_stored_bars and period == AUTOMATIC_SHORT_REFRESH_PERIOD:
        return INITIAL_SERIES_BACKFILL_PERIOD
    return period


async def visible_market_codes(
    database: AsyncSession,
    organization_id: uuid.UUID,
) -> set[str]:
    hidden_codes = set(
        (
            await database.scalars(
                select(OrganizationMarketPolicy.market_code).where(
                    OrganizationMarketPolicy.organization_id == organization_id,
                    OrganizationMarketPolicy.is_visible.is_(False),
                )
            )
        ).all()
    )
    all_codes = set((await database.scalars(select(Market.code))).all())
    return all_codes - hidden_codes


async def is_market_visible(
    database: AsyncSession,
    organization_id: uuid.UUID,
    market_code: str,
) -> bool:
    return market_code in await visible_market_codes(database, organization_id)


async def market_responses(
    database: AsyncSession,
    organization_id: uuid.UUID,
    *,
    visible_only: bool,
) -> list[MarketResponse]:
    # Navigation follows the catalog order (macro, crypto, forex, US, HK, CN,
    # TW, TW derivatives), not the alphabetical order of the codes.
    catalog_order = {definition.code: index for index, definition in enumerate(MARKETS)}
    markets = sorted(
        (await database.scalars(select(Market))).all(),
        key=lambda market: catalog_order.get(market.code, len(catalog_order)),
    )
    policies = {
        policy.market_code: policy
        for policy in (
            await database.scalars(
                select(OrganizationMarketPolicy).where(
                    OrganizationMarketPolicy.organization_id == organization_id
                )
            )
        ).all()
    }
    responses = []
    for market in markets:
        policy = policies.get(market.code)
        responses.append(
            MarketResponse(
                code=market.code,
                name_en=market.name_en,
                name_zh_hant=market.name_zh_hant,
                name_zh_hans=market.name_zh_hans,
                is_visible=True if policy is None else policy.is_visible,
            )
        )
    return [market for market in responses if market.is_visible] if visible_only else responses


def _bar_response(bar: IndexDailyBar) -> IndexDailyBarResponse:
    return IndexDailyBarResponse(
        symbol=bar.symbol,
        market_code=bar.market_code,
        trade_date=bar.trade_date,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


async def latest_index_bars(
    database: AsyncSession,
    *,
    market_codes: set[str],
) -> list[IndexLatestBarResponse]:
    """The newest bar per symbol in the given markets, in TRACKED_INDICES order.

    Which market an index belongs to is settled by the catalog, so the symbols
    are chosen up front and each one's two newest rows are fetched through the
    primary key. Ranking the whole table with a window function instead made
    the cost grow with history: at ten years of bars it sorted 243k rows and
    spilled to disk on every request, for the twenty rows this returns.
    """
    wanted_entries = [
        (symbol, market) for symbol, market in TRACKED_INDICES.items() if market in market_codes
    ]
    if not wanted_entries:
        return []
    symbols = [symbol for symbol, _market in wanted_entries]
    wanted = values(column("symbol", String), column("market_code", String), name="wanted").data(
        wanted_entries
    )
    two_newest = (
        select(IndexDailyBar)
        .where(
            IndexDailyBar.symbol == wanted.c.symbol,
            IndexDailyBar.market_code == wanted.c.market_code,
        )
        .order_by(IndexDailyBar.trade_date.desc())
        .limit(2)
        .lateral()
    )
    bar = aliased(IndexDailyBar, two_newest)
    rows = (await database.scalars(select(bar).select_from(wanted).join(two_newest, true()))).all()

    # Ordering inside a lateral join is not a promise the outer query keeps, so
    # the pair is put back in order here rather than trusted to arrive that way.
    by_symbol: dict[str, list[IndexDailyBar]] = {}
    for row in rows:
        by_symbol.setdefault(row.symbol, []).append(row)
    responses = []
    for symbol in symbols:
        pair = sorted(by_symbol.get(symbol, []), key=lambda row: row.trade_date, reverse=True)
        if not pair:
            continue
        responses.append(
            IndexLatestBarResponse(
                **_bar_response(pair[0]).model_dump(),
                previous_close=pair[1].close if len(pair) > 1 else None,
            )
        )
    return responses


async def index_daily_bars(
    database: AsyncSession,
    *,
    symbol: str,
    market_code: str,
    start: date,
    end: date,
) -> list[IndexDailyBarResponse]:
    bars = await database.scalars(
        select(IndexDailyBar)
        .where(
            IndexDailyBar.symbol == symbol,
            IndexDailyBar.market_code == market_code,
            IndexDailyBar.trade_date >= start,
            IndexDailyBar.trade_date <= end,
        )
        .order_by(IndexDailyBar.trade_date)
    )
    return [_bar_response(bar) for bar in bars]


def index_moving_averages_response(
    *,
    symbol: str,
    market_code: str,
    requested_bars: Sequence[IndexDailyBar],
    warmup_bars: Sequence[IndexDailyBar],
) -> IndexMovingAveragesResponse:
    """Calculate fixed SMAs without emitting dates outside the requested range.

    A trading session is a stored settled daily bar, so gaps such as weekends
    and exchange holidays never produce calendar filler points.  The caller
    supplies at most 239 earlier sessions: enough context for the longest
    (240-session) period while keeping the response query bounded.
    """
    periods = tuple(MOVING_AVERAGE_PERIODS)
    values: dict[int, list[Decimal]] = {period: [] for period in periods}
    points: dict[int, list[IndexMovingAveragePointResponse]] = {period: [] for period in periods}
    for is_requested, bars in ((False, warmup_bars), (True, requested_bars)):
        for bar in bars:
            for period in periods:
                window = values[period]
                window.append(bar.close)
                if len(window) > period:
                    window.pop(0)
                if is_requested:
                    value = (
                        (sum(window) / Decimal(period)).quantize(
                            MOVING_AVERAGE_QUANTUM, rounding=ROUND_HALF_EVEN
                        )
                        if len(window) == period
                        else None
                    )
                    points[period].append(
                        IndexMovingAveragePointResponse(trade_date=bar.trade_date, value=value)
                    )
    return IndexMovingAveragesResponse(
        symbol=symbol,
        market_code=market_code,
        method="sma",
        price_field="close",
        formula_version="sma-close-v1",
        as_of=requested_bars[-1].trade_date if requested_bars else None,
        series=(
            IndexMovingAverage20SeriesResponse(period=20, points=points[20]),
            IndexMovingAverage60SeriesResponse(period=60, points=points[60]),
            IndexMovingAverage120SeriesResponse(period=120, points=points[120]),
            IndexMovingAverage240SeriesResponse(period=240, points=points[240]),
        ),
    )


async def index_moving_averages(
    database: AsyncSession,
    *,
    symbol: str,
    market_code: str,
    start: date,
    end: date,
) -> IndexMovingAveragesResponse:
    requested_bars = list(
        (
            await database.scalars(
                select(IndexDailyBar)
                .where(
                    IndexDailyBar.symbol == symbol,
                    IndexDailyBar.market_code == market_code,
                    IndexDailyBar.trade_date >= start,
                    IndexDailyBar.trade_date <= end,
                )
                .order_by(IndexDailyBar.trade_date)
            )
        ).all()
    )
    warmup_bars = list(
        (
            await database.scalars(
                select(IndexDailyBar)
                .where(
                    IndexDailyBar.symbol == symbol,
                    IndexDailyBar.market_code == market_code,
                    IndexDailyBar.trade_date < start,
                )
                .order_by(IndexDailyBar.trade_date.desc())
                .limit(MOVING_AVERAGE_WARMUP_SESSIONS)
            )
        ).all()
    )
    warmup_bars.reverse()
    return index_moving_averages_response(
        symbol=symbol,
        market_code=market_code,
        requested_bars=requested_bars,
        warmup_bars=warmup_bars,
    )


async def store_index_daily_bars(
    database: AsyncSession,
    *,
    bars: Sequence[DailyBar],
    provider: str,
    contract_version: str,
    source_fetched_at: datetime,
) -> int:
    """Upsert settled daily bars, keyed on (symbol, trade_date).

    Re-fetching an overlapping window rewrites the same rows instead of adding
    duplicates, so a nightly 7d run and the original 2y backfill can coexist.
    """
    if not bars:
        return 0
    rows = []
    for bar in bars:
        expected_market = TRACKED_INDICES[bar.symbol] if bar.symbol in TRACKED_INDICES else None
        if expected_market != bar.market:
            raise ValueError(
                f"{bar.symbol} market {bar.market!r} does not match its catalog market "
                f"{expected_market!r}"
            )
        if bar.close is None:
            # The adapter rejects these; this guard fails loudly if that changes.
            raise ValueError(f"{bar.symbol} {bar.trade_date} has no close")
        rows.append(
            {
                "symbol": bar.symbol,
                "trade_date": bar.trade_date,
                "market_code": bar.market,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "provider": provider,
                "contract_version": contract_version,
                "source_fetched_at": source_fetched_at,
            }
        )

    # Claim ownership in a stable order. PostgreSQL's unique-index conflict
    # waits serialize concurrent first writers; sorting prevents two multi-symbol
    # callers from waiting on the same ownership rows in opposite order.
    symbols = sorted({bar.symbol for bar in bars})
    ownership_statement = insert(IndexDailyBarSeries).values(
        [{"symbol": symbol, "provider": provider} for symbol in symbols]
    )
    await database.execute(
        ownership_statement.on_conflict_do_nothing(index_elements=[IndexDailyBarSeries.symbol])
    )
    ownership = {
        series.symbol: series.provider
        for series in (
            await database.scalars(
                select(IndexDailyBarSeries)
                .where(IndexDailyBarSeries.symbol.in_(symbols))
                .order_by(IndexDailyBarSeries.symbol)
                .with_for_update()
            )
        ).all()
    }
    if set(ownership) != set(symbols):
        raise RuntimeError("failed to establish index daily-bar provider ownership")
    for symbol in symbols:
        existing_provider = ownership[symbol]
        if existing_provider == provider:
            continue
        has_bars = await database.scalar(
            select(IndexDailyBar.symbol).where(IndexDailyBar.symbol == symbol).limit(1)
        )
        if has_bars is not None:
            raise IndexProviderConflictError(
                f"refusing to add {provider!r} bars to {symbol}: its series belongs to "
                f"provider {existing_provider!r}; delete the old rows first to switch providers"
            )
        # Preserve the documented switch path. The ownership row is locked, and
        # no child bars remain, so changing the claim and inserting the new
        # provider's rows is atomic inside the caller's transaction.
        await database.execute(
            update(IndexDailyBarSeries)
            .where(IndexDailyBarSeries.symbol == symbol)
            .values(provider=provider)
        )
    # A multi-row INSERT binds one parameter per column per row, and PostgreSQL's
    # wire protocol caps a statement at 65535 of them. `period=max` returns 24k+
    # rows for ^GSPC, so the write is chunked. The chunk size is derived from the
    # column count rather than hardcoded, so it still holds if a column is added.
    chunk_size = MAX_BIND_PARAMETERS // len(rows[0])
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        statement = insert(IndexDailyBar).values(chunk)
        result: Result[Any] = await database.execute(
            statement.on_conflict_do_update(
                index_elements=["symbol", "trade_date"],
                set_={
                    "market_code": statement.excluded.market_code,
                    "open": statement.excluded.open,
                    "high": statement.excluded.high,
                    "low": statement.excluded.low,
                    "close": statement.excluded.close,
                    "volume": statement.excluded.volume,
                    "contract_version": statement.excluded.contract_version,
                    "source_fetched_at": statement.excluded.source_fetched_at,
                    "updated_at": func.now(),
                },
                # A row keeps the provider it was created with. Without this the
                # upsert would let a second provider overwrite an existing
                # series day by day, which is the splicing that
                # docs/architecture/twelve-data-three-market-morning-report-plan.md
                # forbids, and it would leave no trace that it happened.
                where=IndexDailyBar.provider == statement.excluded.provider,
            ).returning(literal_column("1"))
        )
        # Rows skipped by that WHERE are neither inserted nor updated, so a short
        # count is the only signal that a foreign provider was refused. RETURNING
        # is what reports it: `rowcount` is -1 on this driver.
        written = len(result.all())
        if written != len(chunk):
            raise ValueError(
                f"refusing to overwrite {len(chunk) - written} existing "
                f"index_daily_bars rows with provider {provider!r}: a symbol's series "
                "belongs to one provider, so switch it by deleting the old rows first"
            )
    return len(rows)


@dataclass(frozen=True, slots=True)
class IndexRefresh:
    result: YfinanceDailyBars
    stored_count: int


@dataclass(frozen=True, slots=True)
class IndexRefreshFailure:
    symbol: str
    market: MarketCode
    error: str


@dataclass(frozen=True, slots=True)
class TaiexRefresh:
    stored_count: int
    as_of: date
    fetched_at: datetime
    # Months that failed, as "YYYY-MM: reason". Non-empty means the refresh was
    # partial: the months that did work are stored, and the caller reports which
    # did not so a later run can be pointed at them.
    failed_months: tuple[str, ...] = ()
    # True when the walk stopped early because TWSE looked down rather than
    # because one month was bad.
    aborted: bool = False


def taiex_months(*, start: date, end: date) -> tuple[date, ...]:
    """First-of-month markers covering `start`..`end` inclusive.

    TWSE keys both TAIEX reports on any date inside the wanted month, so the
    caller walks months rather than trading days.
    """
    if start > end:
        raise ValueError("start must not be after end")
    months: list[date] = []
    cursor = start.replace(day=1)
    last = end.replace(day=1)
    while cursor <= last:
        months.append(cursor)
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return tuple(months)


async def select_taiex_refresh_months(
    database: AsyncSession, *, today: date, requested_months: int
) -> tuple[date, ...]:
    """Widen an incremental request to the full backfill when the series is empty.

    Mirrors `select_index_refresh_period` for the Yahoo path. An explicit longer
    request is left alone; only the incremental window is widened, so a CLI
    backfill still means exactly what it asked for.
    """
    if requested_months < 1:
        raise ValueError("requested_months must be at least 1")
    months_back = requested_months
    if requested_months == TAIEX_INCREMENTAL_MONTHS:
        has_stored_bars = await database.scalar(
            select(IndexDailyBar.symbol).where(IndexDailyBar.symbol == TAIEX_SYMBOL).limit(1)
        )
        if has_stored_bars is None:
            months_back = TAIEX_INITIAL_BACKFILL_MONTHS
    start = today.replace(day=1)
    for _ in range(months_back - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    return taiex_months(start=start, end=today)


async def refresh_taiex_daily_bars(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    adapter: TwseAdapter,
    months: Sequence[date],
) -> TaiexRefresh:
    """Fetch and upsert ^TWII one month at a time.

    Takes a session factory rather than a session because the fetching is slow
    and the writing is not: TWSE spaces its own requests, so a 25-month
    backfill is minutes of network I/O. Holding one transaction across all of
    it would pin a connection, keep every stored month invisible and
    rollback-able until the very end, and hold a lock the whole time. Instead
    each month is fetched outside any transaction and then written in its own,
    taking `TAIEX_REFRESH_LOCK_KEY` for that write alone. A month that lands is
    committed and stays.

    A month that fails is reported and the walk continues, because a bootstrap
    that discarded everything over one bad month could never recover: an empty
    series widens the window back to the same months on the next run and meets
    the same failure again. That mirrors the Yahoo path, where one dead symbol
    does not discard the ones that resolved.
    """
    if not months:
        raise ValueError("months must not be empty")
    stored_count = 0
    as_of: date | None = None
    fetched_at: datetime | None = None
    failed_months: list[str] = []
    consecutive_failures = 0
    aborted = False
    for month in months:
        # Outside any transaction: this is the slow part.
        try:
            fetched = await adapter.get_taiex_daily_bars(month)
        except DataSourceError as error:
            failed_months.append(f"{month:%Y-%m}: {type(error).__name__}: {error}")
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_TAIEX_FAILURES:
                # The source is down, not this one month. Asking the rest would
                # cost minutes of spaced requests and tell us nothing.
                aborted = True
                break
            continue
        consecutive_failures = 0
        if not fetched.items:
            # A month-wide request answers an unpublished or future month with
            # an empty payload rather than an error. Nothing to store, and not
            # a failure worth reporting.
            continue
        bars = [
            DailyBar(
                instrument_source_id=TAIEX_SYMBOL,
                market=TRACKED_INDICES[TAIEX_SYMBOL],
                symbol=TAIEX_SYMBOL,
                trade_date=bar.trade_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                source=TAIEX_PROVIDER,
            )
            for bar in fetched.items
        ]
        try:
            async with session_factory.begin() as database:
                # Transaction-scoped: released on commit or rollback, so it
                # cannot be stranded the way a session-scoped lock can.
                await database.execute(select(func.pg_advisory_xact_lock(TAIEX_REFRESH_LOCK_KEY)))
                stored_count += await store_index_daily_bars(
                    database,
                    bars=bars,
                    provider=TAIEX_PROVIDER,
                    contract_version=TAIEX_CONTRACT_VERSION,
                    source_fetched_at=fetched.fetched_at,
                )
        except (IntegrityError, DataError) as error:
            # A value the adapter let through that the schema will not hold.
            # `IndexProviderConflictError` is deliberately not caught: the
            # series belonging to another provider is true for every month, so
            # retrying the remaining ones would be pointless.
            failed_months.append(f"{month:%Y-%m}: {type(error).__name__}: {error.orig}")
            continue
        fetched_at = fetched.fetched_at
        month_as_of = max(bar.trade_date for bar in bars)
        as_of = month_as_of if as_of is None else max(as_of, month_as_of)

    if as_of is None or fetched_at is None:
        raise DataSourceContractError(
            "twse stored no TAIEX sessions for any of the "
            f"{len(months)} requested month(s): "
            + ("; ".join(failed_months) if failed_months else "all months were empty")
        )
    return TaiexRefresh(
        stored_count=stored_count,
        as_of=as_of,
        fetched_at=fetched_at,
        failed_months=tuple(failed_months),
        aborted=aborted,
    )


async def refresh_index_daily_bars(
    database: AsyncSession,
    *,
    adapter: YfinanceAdapter,
    symbols: Sequence[IndexSymbol],
    period: str,
) -> tuple[list[IndexRefresh], list[IndexRefreshFailure]]:
    """Fetch and upsert one window for each symbol.

    A symbol that fails is recorded and skipped: one dead upstream response must
    not discard the symbols that did resolve, which matters most for the nightly
    run where nobody is watching.
    """
    # Reject invalid caller input before using a database connection/lock.
    # ^TWII is tracked but not served here: it belongs to TWSE, and letting it
    # through would quietly overwrite the exchange's bars with Yahoo's.
    unsupported = sorted(set(symbols) - set(YFINANCE_INDICES))
    if unsupported:
        raise ValueError(f"symbols not served by yfinance: {', '.join(unsupported)}")
    # One session-scoped lock covers the legacy synchronous endpoint, scheduler
    # and durable worker. It is deliberately acquired before any Yahoo call,
    # because this provider has no published quota and credits/cookies are not
    # safe to duplicate.
    await database.execute(select(func.pg_advisory_lock(YAHOO_REFRESH_LOCK_KEY)))
    try:
        return await _refresh_index_daily_bars_unlocked(
            database, adapter=adapter, symbols=symbols, period=period
        )
    finally:
        await database.execute(select(func.pg_advisory_unlock(YAHOO_REFRESH_LOCK_KEY)))


async def _refresh_index_daily_bars_unlocked(
    database: AsyncSession,
    *,
    adapter: YfinanceAdapter,
    symbols: Sequence[IndexSymbol],
    period: str,
) -> tuple[list[IndexRefresh], list[IndexRefreshFailure]]:
    # Typing keeps checked callers honest; this guard keeps an untyped one from
    # reaching a bare KeyError on the lookup below.
    # ^TWII is tracked but not served here: it belongs to TWSE, and letting it
    # through would quietly overwrite the exchange's bars with Yahoo's.
    unsupported = sorted(set(symbols) - set(YFINANCE_INDICES))
    if unsupported:
        raise ValueError(f"symbols not served by yfinance: {', '.join(unsupported)}")

    # A newly added tracked symbol has no rows in an existing deployment. A
    # short scheduled refresh alone cannot provide the prior-year close needed
    # for YTD calculations, so give only missing series the normal two-year
    # bootstrap. This is deliberately one bounded request per symbol: explicit
    # long-window operator requests remain unchanged and later refreshes return
    # to their requested short window.
    stored_symbols = set(
        (
            await database.scalars(
                select(IndexDailyBar.symbol).where(IndexDailyBar.symbol.in_(symbols)).distinct()
            )
        ).all()
    )
    fetch_periods = {
        symbol: select_index_refresh_period(period=period, has_stored_bars=symbol in stored_symbols)
        for symbol in symbols
    }

    # Fetching is the slow part: yfinance issues several HTTP requests per symbol
    # (timezone, cookie/crumb, then the bars), so ten symbols in series can
    # outlast the 60s proxy budget in infra/nginx/conf.d/default.conf whenever
    # Yahoo is slow. Bounded concurrency mirrors TwelveDataTransport.
    semaphore = asyncio.Semaphore(MAX_FETCH_CONCURRENCY)

    async def fetch(symbol: IndexSymbol) -> YfinanceDailyBars | DataSourceError:
        async with semaphore:
            try:
                return await adapter.get_daily_bars(
                    market=TRACKED_INDICES[symbol],
                    symbol=symbol,
                    period=fetch_periods[symbol],
                )
            except DataSourceError as error:
                return error

    fetched = await asyncio.gather(*(fetch(symbol) for symbol in symbols))

    refreshed: list[IndexRefresh] = []
    failures: list[IndexRefreshFailure] = []
    # Writes stay sequential and in request order. An AsyncSession is not safe
    # for concurrent use, so only the fetches above run in parallel.
    for symbol, outcome in zip(symbols, fetched, strict=True):
        market = TRACKED_INDICES[symbol]
        if isinstance(outcome, DataSourceError):
            failures.append(IndexRefreshFailure(symbol=symbol, market=market, error=str(outcome)))
            continue
        result = outcome
        # Each symbol writes inside its own savepoint. A row that still trips a
        # CHECK or foreign key would otherwise abort the surrounding
        # transaction, discarding every symbol written before it and leaving the
        # session unusable for the ones after.
        try:
            async with database.begin_nested():
                stored_count = await store_index_daily_bars(
                    database,
                    bars=result.items,
                    provider=result.provenance.provider,
                    contract_version=result.provenance.contract_version,
                    source_fetched_at=result.provenance.fetched_at,
                )
        except (IntegrityError, DataError, ValueError) as error:
            detail = error.orig if isinstance(error, (IntegrityError, DataError)) else error
            failures.append(
                IndexRefreshFailure(
                    symbol=symbol,
                    market=market,
                    error=f"{type(error).__name__}: {detail}",
                )
            )
            continue
        refreshed.append(IndexRefresh(result=result, stored_count=stored_count))
    return refreshed, failures


async def stored_flow_dates(
    database: AsyncSession,
    *,
    flows: type[InstitutionalMarketFlow] | type[InstitutionalStockFlow],
    market_code: str,
    on_or_before: date,
    limit: int,
) -> set[date]:
    """The `limit` most recent trading dates that already hold rows in `flows`."""
    rows = await database.scalars(
        select(flows.trade_date)
        .where(flows.market_code == market_code, flows.trade_date <= on_or_before)
        .distinct()
        .order_by(flows.trade_date.desc())
        .limit(limit)
    )
    return set(rows.all())


async def store_institutional_market_flows(
    database: AsyncSession, *, market_code: str, flows: TwseMarketFlows
) -> int:
    """Upsert one day's market-level flows, keyed on (trade_date, investor_type)."""
    if not flows.items:
        return 0
    statement = insert(InstitutionalMarketFlow).values(
        [
            {
                "trade_date": flows.trade_date,
                "investor_type": item.investor_type,
                "market_code": market_code,
                "buy_amount": item.buy_amount,
                "sell_amount": item.sell_amount,
                "net_amount": item.net_amount,
                "source_fetched_at": flows.fetched_at,
            }
            for item in flows.items
        ]
    )
    await database.execute(
        statement.on_conflict_do_update(
            index_elements=["trade_date", "investor_type"],
            set_={
                "market_code": statement.excluded.market_code,
                "buy_amount": statement.excluded.buy_amount,
                "sell_amount": statement.excluded.sell_amount,
                "net_amount": statement.excluded.net_amount,
                "source_fetched_at": statement.excluded.source_fetched_at,
                "updated_at": func.now(),
            },
        )
    )
    return len(flows.items)


async def store_institutional_stock_flows(
    database: AsyncSession, *, market_code: str, flows: TwseStockFlows
) -> int:
    """Upsert one day's per-stock flows, keyed on (trade_date, symbol, investor_type)."""
    if not flows.items:
        return 0
    rows = [
        {
            "trade_date": flows.trade_date,
            "symbol": item.symbol,
            "investor_type": item.investor_type,
            "market_code": market_code,
            "security_name": item.security_name,
            "buy_shares": item.buy_shares,
            "sell_shares": item.sell_shares,
            "net_shares": item.net_shares,
            "source_fetched_at": flows.fetched_at,
        }
        for item in flows.items
    ]
    # ~1,340 securities x 5 investors x 9 columns is close to the wire-protocol
    # bind-parameter cap, so the write is chunked like the index bars.
    chunk_size = MAX_BIND_PARAMETERS // len(rows[0])
    for start in range(0, len(rows), chunk_size):
        statement = insert(InstitutionalStockFlow).values(rows[start : start + chunk_size])
        await database.execute(
            statement.on_conflict_do_update(
                index_elements=["trade_date", "symbol", "investor_type"],
                set_={
                    "market_code": statement.excluded.market_code,
                    "security_name": statement.excluded.security_name,
                    "buy_shares": statement.excluded.buy_shares,
                    "sell_shares": statement.excluded.sell_shares,
                    "net_shares": statement.excluded.net_shares,
                    "source_fetched_at": statement.excluded.source_fetched_at,
                    "updated_at": func.now(),
                },
            )
        )
    return len(rows)


# The one market these flows are collected for. data_management passes it back
# in when it stores a run's rows, so it lives with the tables it belongs to.
INSTITUTIONAL_MARKET_CODE = "tw_equity"
# The five stored categories folded into the three the product reports on.
# foreign_dealer goes with foreign, never with the dealer books; see the note on
# INVESTOR_TYPES in models.py. TWSE has reported it as 0 on every day observed so
# far, so nothing in the stored data pins this down numerically.
INSTITUTIONAL_FOREIGN_TYPES = ("foreign", "foreign_dealer")
INSTITUTIONAL_TRUST_TYPES = ("trust",)
INSTITUTIONAL_DEALER_TYPES = ("dealer_self", "dealer_hedge")
# A sixth category would otherwise be dropped from the totals in silence. Import
# fails instead, before anything can serve a number that is short one investor.
# Sorted rather than set-compared so a category in two folds, which would double
# count it, is caught too.
if sorted(
    INSTITUTIONAL_FOREIGN_TYPES + INSTITUTIONAL_TRUST_TYPES + INSTITUTIONAL_DEALER_TYPES
) != sorted(INVESTOR_TYPES):
    raise RuntimeError("the institutional folds must cover every investor type exactly once")
# TWSE reports market flows in TWD and stock flows in shares; the dashboards
# read 億元 and 張.
HUNDRED_MILLION = Decimal(100_000_000)
SHARES_PER_LOT = Decimal(1_000)


def _net_of(column: Any, investor_types: Sequence[str]) -> Any:
    return func.coalesce(
        func.sum(column).filter(InstitutionalMarketFlow.investor_type.in_(investor_types)), 0
    )


def _stock_net_of(column: Any, investor_types: Sequence[str]) -> Any:
    return func.coalesce(
        func.sum(column).filter(InstitutionalStockFlow.investor_type.in_(investor_types)), 0
    )


async def institutional_flow_series(
    database: AsyncSession, *, market_code: str, start: date, end: date
) -> list[InstitutionalFlowPointResponse]:
    """One point per stored trading day in the window, oldest first, in 億元.

    The five stored categories are folded into the three the dashboard shows,
    and `total` is all five added up, which is TWSE's own 合計 row.
    """
    net = InstitutionalMarketFlow.net_amount
    rows = await database.execute(
        select(
            InstitutionalMarketFlow.trade_date,
            _net_of(net, INSTITUTIONAL_FOREIGN_TYPES).label("foreign_net"),
            _net_of(net, INSTITUTIONAL_TRUST_TYPES).label("trust_net"),
            _net_of(net, INSTITUTIONAL_DEALER_TYPES).label("dealer_net"),
            func.coalesce(func.sum(net), 0).label("total_net"),
        )
        .where(
            InstitutionalMarketFlow.market_code == market_code,
            InstitutionalMarketFlow.trade_date >= start,
            InstitutionalMarketFlow.trade_date <= end,
        )
        .group_by(InstitutionalMarketFlow.trade_date)
        .order_by(InstitutionalMarketFlow.trade_date)
    )
    return [
        InstitutionalFlowPointResponse(
            trade_date=row.trade_date,
            foreign=Decimal(row.foreign_net) / HUNDRED_MILLION,
            trust=Decimal(row.trust_net) / HUNDRED_MILLION,
            dealer=Decimal(row.dealer_net) / HUNDRED_MILLION,
            total=Decimal(row.total_net) / HUNDRED_MILLION,
        )
        for row in rows
    ]


async def institutional_stock_rows(
    database: AsyncSession, *, market_code: str, on_or_before: date
) -> tuple[date | None, list[InstitutionalStockFlowResponse]]:
    """The most recent stored day at or before `on_or_before`, in 張.

    Every security of that day, largest net buy first, so the caller decides how
    many of the two ends to show.
    """
    day = InstitutionalStockFlow.trade_date
    shares = InstitutionalStockFlow.net_shares
    latest = (
        select(func.max(day))
        .where(InstitutionalStockFlow.market_code == market_code, day <= on_or_before)
        .scalar_subquery()
    )
    total_shares = func.sum(shares).label("total_shares")
    rows = (
        await database.execute(
            select(
                day,
                InstitutionalStockFlow.symbol,
                InstitutionalStockFlow.security_name,
                _stock_net_of(shares, INSTITUTIONAL_FOREIGN_TYPES).label("foreign_shares"),
                _stock_net_of(shares, INSTITUTIONAL_TRUST_TYPES).label("trust_shares"),
                _stock_net_of(shares, INSTITUTIONAL_DEALER_TYPES).label("dealer_shares"),
                total_shares,
            )
            .where(InstitutionalStockFlow.market_code == market_code, day == latest)
            # The name is functionally dependent on the symbol within a day, but
            # PostgreSQL only accepts that for a primary key, which this is not.
            .group_by(day, InstitutionalStockFlow.symbol, InstitutionalStockFlow.security_name)
            # The symbol tie-break keeps the order stable across identical sums,
            # which a day of untraded securities has plenty of.
            .order_by(total_shares.desc(), InstitutionalStockFlow.symbol)
        )
    ).all()
    if not rows:
        return None, []
    return rows[0].trade_date, [
        InstitutionalStockFlowResponse(
            symbol=row.symbol,
            name=row.security_name,
            foreign_lots=Decimal(row.foreign_shares) / SHARES_PER_LOT,
            trust_lots=Decimal(row.trust_shares) / SHARES_PER_LOT,
            dealer_lots=Decimal(row.dealer_shares) / SHARES_PER_LOT,
            total_lots=Decimal(row.total_shares) / SHARES_PER_LOT,
        )
        for row in rows
    ]
