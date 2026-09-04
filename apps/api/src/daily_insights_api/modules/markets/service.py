import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Result, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.data_sources.api import (
    TRACKED_INDICES,
    DailyBar,
    DataSourceError,
    IndexSymbol,
    MarketCode,
    YfinanceAdapter,
    YfinanceDailyBars,
)
from daily_insights_api.modules.markets.models import (
    IndexDailyBar,
    IndexDailyBarSeries,
    Market,
    OrganizationMarketPolicy,
)
from daily_insights_api.modules.markets.schemas import MarketResponse

MAX_BIND_PARAMETERS = 65535
# Yahoo publishes no rate limit and is reached through a scraping client, so
# this stays conservative; it matches TwelveDataTransport's default.
MAX_FETCH_CONCURRENCY = 4


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
    markets = (await database.scalars(select(Market).order_by(Market.code))).all()
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
        except (IntegrityError, DataError, IndexProviderConflictError) as error:
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
