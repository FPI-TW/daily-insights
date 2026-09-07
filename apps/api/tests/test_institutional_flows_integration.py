import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from conftest import remigrate_database, reset_database_schema
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api.modules.data_sources.api import (
    TwseMarketFlow,
    TwseMarketFlows,
    TwseStockFlow,
    TwseStockFlows,
)
from daily_insights_api.modules.markets.api import (
    store_institutional_market_flows,
    store_institutional_stock_flows,
    stored_flow_dates,
)
from daily_insights_api.modules.markets.models import (
    InstitutionalMarketFlow,
    InstitutionalStockFlow,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    await asyncio.to_thread(remigrate_database, database_url)
    engine = create_async_engine(database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            await engine.dispose()
        finally:
            await asyncio.to_thread(reset_database_schema, database_url)


def _market(trade_date: date, foreign_net: int, fetched_at: datetime) -> TwseMarketFlows:
    return TwseMarketFlows(
        trade_date=trade_date,
        items=(
            TwseMarketFlow("foreign", 100 + foreign_net, 100, foreign_net),
            TwseMarketFlow("trust", 10, 4, 6),
        ),
        fetched_at=fetched_at,
    )


@pytest.mark.asyncio
async def test_refetching_a_day_rewrites_rows_instead_of_duplicating(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = datetime(2026, 9, 4, 8, tzinfo=UTC)
    second = datetime(2026, 9, 5, 8, tzinfo=UTC)
    async with session_factory.begin() as database:
        assert (
            await store_institutional_market_flows(
                database, market_code="tw_equity", flows=_market(date(2026, 9, 4), 5, first)
            )
            == 2
        )
        await store_institutional_market_flows(
            database, market_code="tw_equity", flows=_market(date(2026, 9, 3), 1, first)
        )
    async with session_factory.begin() as database:
        await store_institutional_market_flows(
            database, market_code="tw_equity", flows=_market(date(2026, 9, 4), 7, second)
        )
        stock = TwseStockFlows(
            trade_date=date(2026, 9, 4),
            items=(TwseStockFlow("2324", "仁寶", "foreign", 30, 10, 20),),
            fetched_at=second,
        )
        assert (
            await store_institutional_stock_flows(database, market_code="tw_equity", flows=stock)
            == 1
        )
        await store_institutional_stock_flows(database, market_code="tw_equity", flows=stock)

    async with session_factory() as database:
        assert await database.scalar(select(func.count()).select_from(InstitutionalMarketFlow)) == 4
        foreign = await database.scalar(
            select(InstitutionalMarketFlow).where(
                InstitutionalMarketFlow.trade_date == date(2026, 9, 4),
                InstitutionalMarketFlow.investor_type == "foreign",
            )
        )
        assert foreign is not None
        assert foreign.net_amount == 7 and foreign.source_fetched_at == second
        assert await database.scalar(select(func.count()).select_from(InstitutionalStockFlow)) == 1
        assert await stored_flow_dates(
            database,
            flows=InstitutionalMarketFlow,
            market_code="tw_equity",
            on_or_before=date(2026, 9, 4),
            limit=1,
        ) == {date(2026, 9, 4)}
        assert await stored_flow_dates(
            database,
            flows=InstitutionalMarketFlow,
            market_code="tw_equity",
            on_or_before=date(2026, 9, 3),
            limit=40,
        ) == {date(2026, 9, 3)}
        assert await stored_flow_dates(
            database,
            flows=InstitutionalStockFlow,
            market_code="tw_equity",
            on_or_before=date(2026, 9, 4),
            limit=7,
        ) == {date(2026, 9, 4)}
