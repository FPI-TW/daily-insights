import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.data_sources.api import (
    TRACKED_INDICES,
    DailyBar,
    DailyBarsResult,
    DataSourceError,
    MarketCode,
    YfinanceAdapter,
)
from daily_insights_api.modules.markets.models import (
    IndexDailyBar,
    Market,
    OrganizationMarketPolicy,
)
from daily_insights_api.modules.markets.schemas import MarketResponse


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
    statement = insert(IndexDailyBar).values(rows)
    await database.execute(
        statement.on_conflict_do_update(
            index_elements=["symbol", "trade_date"],
            set_={
                "market_code": statement.excluded.market_code,
                "open": statement.excluded.open,
                "high": statement.excluded.high,
                "low": statement.excluded.low,
                "close": statement.excluded.close,
                "volume": statement.excluded.volume,
                "provider": statement.excluded.provider,
                "contract_version": statement.excluded.contract_version,
                "source_fetched_at": statement.excluded.source_fetched_at,
                "updated_at": func.now(),
            },
        )
    )
    return len(rows)


@dataclass(frozen=True, slots=True)
class IndexRefresh:
    result: DailyBarsResult
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
    symbols: Sequence[str],
    period: str,
) -> tuple[list[IndexRefresh], list[IndexRefreshFailure]]:
    """Fetch and upsert one window for each symbol.

    A symbol that fails is recorded and skipped: one dead upstream response must
    not discard the symbols that did resolve, which matters most for the nightly
    run where nobody is watching.
    """
    refreshed: list[IndexRefresh] = []
    failures: list[IndexRefreshFailure] = []
    for symbol in symbols:
        market = TRACKED_INDICES[symbol]
        try:
            result = await adapter.get_daily_bars(market=market, symbol=symbol, period=period)
        except DataSourceError as error:
            failures.append(IndexRefreshFailure(symbol=symbol, market=market, error=str(error)))
            continue
        stored_count = await store_index_daily_bars(
            database,
            bars=result.items,
            provider=result.provenance.provider,
            contract_version=result.provenance.contract_version,
            source_fetched_at=result.provenance.fetched_at,
        )
        refreshed.append(IndexRefresh(result=result, stored_count=stored_count))
    return refreshed, failures
