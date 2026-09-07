import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import Result, String, column, func, literal_column, select, true, update, values
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from daily_insights_api.modules.data_sources.api import (
    TRACKED_INDICES,
    DailyBar,
    DataSourceError,
    IndexSymbol,
    MarketCode,
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
    InstitutionalMarketFlowResponse,
    InstitutionalStockFlowLeaderResponse,
    InstitutionalStockFlowLeadersResponse,
    MarketResponse,
)

MAX_BIND_PARAMETERS = 65535
# Yahoo publishes no rate limit and is reached through a scraping client, so
# this stays conservative; it matches TwelveDataTransport's default.
MAX_FETCH_CONCURRENCY = 4
YAHOO_REFRESH_LOCK_KEY = 4_741_901_938_764_211_037
MOVING_AVERAGE_PERIODS = (20, 60, 120, 240)
MOVING_AVERAGE_WARMUP_SESSIONS = max(MOVING_AVERAGE_PERIODS) - 1
MOVING_AVERAGE_QUANTUM = Decimal("0.0000000001")


class IndexProviderConflictError(ValueError):
    """A symbol already has durable bars owned by another provider."""


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
    untracked = sorted(set(symbols) - set(TRACKED_INDICES))
    if untracked:
        raise ValueError(f"untracked symbols: {', '.join(untracked)}")
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
    untracked = sorted(set(symbols) - set(TRACKED_INDICES))
    if untracked:
        raise ValueError(f"untracked symbols: {', '.join(untracked)}")

    # Fetching is the slow part: yfinance issues several HTTP requests per symbol
    # (timezone, cookie/crumb, then the bars), so ten symbols in series can
    # outlast the 60s proxy budget in infra/nginx/conf.d/default.conf whenever
    # Yahoo is slow. Bounded concurrency mirrors TwelveDataTransport.
    semaphore = asyncio.Semaphore(MAX_FETCH_CONCURRENCY)

    async def fetch(symbol: IndexSymbol) -> YfinanceDailyBars | DataSourceError:
        async with semaphore:
            try:
                return await adapter.get_daily_bars(
                    market=TRACKED_INDICES[symbol], symbol=symbol, period=period
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
# A sixth category would otherwise be dropped from the market totals in silence.
# Import fails instead, before anything can serve a number that is short one
# investor. Sorted rather than set-compared so a category in two folds, which
# would double count it, is caught too.
if sorted(
    INSTITUTIONAL_FOREIGN_TYPES + INSTITUTIONAL_TRUST_TYPES + INSTITUTIONAL_DEALER_TYPES
) != sorted(INVESTOR_TYPES):
    raise RuntimeError("the institutional folds must cover every investor type exactly once")
INSTITUTIONAL_STOCK_LEADERS = 5


async def institutional_market_flows(
    database: AsyncSession,
    *,
    market_code: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[InstitutionalMarketFlowResponse]:
    """Stored trading days, newest first, as three net amounts.

    Both bounds are inclusive, and omitting them returns everything stored: the
    table gains one date per trading day and the run only ever backfills 40, so
    that is hundreds of rows a year, not thousands.
    """

    def net_of(*investor_types: str) -> Any:
        return func.coalesce(
            func.sum(InstitutionalMarketFlow.net_amount).filter(
                InstitutionalMarketFlow.investor_type.in_(investor_types)
            ),
            0,
        )

    statement = select(
        InstitutionalMarketFlow.trade_date,
        net_of(*INSTITUTIONAL_FOREIGN_TYPES).label("foreign_net"),
        net_of(*INSTITUTIONAL_TRUST_TYPES).label("trust_net"),
        net_of(*INSTITUTIONAL_DEALER_TYPES).label("dealer_net"),
    ).where(InstitutionalMarketFlow.market_code == market_code)
    if start_date is not None:
        statement = statement.where(InstitutionalMarketFlow.trade_date >= start_date)
    if end_date is not None:
        statement = statement.where(InstitutionalMarketFlow.trade_date <= end_date)
    rows = await database.execute(
        statement.group_by(InstitutionalMarketFlow.trade_date).order_by(
            InstitutionalMarketFlow.trade_date.desc()
        )
    )
    return [
        InstitutionalMarketFlowResponse(
            trade_date=row.trade_date,
            foreign=row.foreign_net,
            trust=row.trust_net,
            dealer=row.dealer_net,
        )
        for row in rows
    ]


async def institutional_stock_flow_leaders(
    database: AsyncSession, *, market_code: str, trade_date: date | None = None
) -> InstitutionalStockFlowLeadersResponse:
    """The five largest net buys and net sells of one trading day.

    A security's five investor rows are summed first, so the ranking is on what
    the institutions did to that security as a whole rather than on any one
    investor's book.

    One statement aggregates the day, and the two ends are taken here: a day is
    about 1,300 securities, which is cheaper to carry back once than to make the
    database group them a second time.
    """
    day = InstitutionalStockFlow.trade_date
    on_day = (
        day
        == select(func.max(day))
        .where(InstitutionalStockFlow.market_code == market_code)
        .scalar_subquery()
        if trade_date is None
        else day == trade_date
    )
    net_shares = func.sum(InstitutionalStockFlow.net_shares).label("net_shares")
    rows = (
        await database.execute(
            select(
                day, InstitutionalStockFlow.symbol, InstitutionalStockFlow.security_name, net_shares
            )
            .where(InstitutionalStockFlow.market_code == market_code, on_day)
            # The name is functionally dependent on the symbol within a day, but
            # PostgreSQL only accepts that for a primary key, which this is not.
            .group_by(day, InstitutionalStockFlow.symbol, InstitutionalStockFlow.security_name)
        )
    ).all()
    if not rows:
        return InstitutionalStockFlowLeadersResponse(
            trade_date=trade_date, top_buys=[], top_sells=[]
        )

    def leaders(side: list[Any], key: Any) -> list[InstitutionalStockFlowLeaderResponse]:
        # Each list holds its own sign only, so a quiet day returns fewer than
        # five rather than filling the sell list with net buyers, and a total of
        # zero is neither, so it is listed nowhere. The symbol tie-break keeps
        # the order stable across identical sums, which a day of untraded
        # securities has plenty of.
        return [
            InstitutionalStockFlowLeaderResponse(
                symbol=row.symbol,
                security_name=row.security_name,
                net_shares=row.net_shares,
            )
            for row in sorted(side, key=key)[:INSTITUTIONAL_STOCK_LEADERS]
        ]

    return InstitutionalStockFlowLeadersResponse(
        trade_date=rows[0].trade_date,
        top_buys=leaders(
            [row for row in rows if row.net_shares > 0], lambda row: (-row.net_shares, row.symbol)
        ),
        top_sells=leaders(
            [row for row in rows if row.net_shares < 0], lambda row: (row.net_shares, row.symbol)
        ),
    )
