import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api.core.config import get_settings
from daily_insights_api.modules.data_sources.api import (
    DailyBar,
    DailyBarsResult,
    Provenance,
    YfinanceAdapter,
)
from daily_insights_api.modules.markets.api import (
    refresh_index_daily_bars,
    store_index_daily_bars,
)
from daily_insights_api.modules.markets.models import IndexDailyBar

pytestmark = pytest.mark.integration

FETCHED_AT = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)


def _reset_schema(database_url: str) -> None:
    engine = create_sync_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def _remigrate(database_url: str) -> None:
    previous = os.environ.get("DAILY_INSIGHTS_DATABASE_URL")
    os.environ["DAILY_INSIGHTS_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        _reset_schema(database_url)
        command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("DAILY_INSIGHTS_DATABASE_URL", None)
        else:
            os.environ["DAILY_INSIGHTS_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    await asyncio.to_thread(_remigrate, database_url)
    engine = create_async_engine(database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            await engine.dispose()
        finally:
            await asyncio.to_thread(_reset_schema, database_url)


def _bar(trade_date: date, close: str, volume: int | None = 1_000) -> DailyBar:
    return DailyBar(
        instrument_source_id="^TWII",
        market="tw_equity",
        symbol="^TWII",
        trade_date=trade_date,
        open=Decimal("100.0"),
        high=Decimal("101.0"),
        low=Decimal("99.0"),
        close=Decimal(close),
        volume=volume,
        source="yfinance",
    )


async def _store(database: AsyncSession, bars: list[DailyBar]) -> int:
    return await store_index_daily_bars(
        database,
        bars=bars,
        provider="yfinance",
        contract_version="2026-09-03.v1",
        source_fetched_at=FETCHED_AT,
    )


async def test_overlapping_fetches_upsert_instead_of_duplicating(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as database:
        await _store(database, [_bar(date(2026, 9, 1), "100.0"), _bar(date(2026, 9, 2), "200.0")])

    # A later window overlaps 09-02 and carries a corrected close.
    async with session_factory.begin() as database:
        stored = await _store(
            database,
            [_bar(date(2026, 9, 2), "222.5"), _bar(date(2026, 9, 3), "300.0")],
        )
    assert stored == 2

    async with session_factory() as database:
        total = await database.scalar(select(func.count()).select_from(IndexDailyBar))
        assert total == 3
        row = await database.scalar(
            select(IndexDailyBar).where(
                IndexDailyBar.symbol == "^TWII",
                IndexDailyBar.trade_date == date(2026, 9, 2),
            )
        )
        assert row is not None
        assert row.close == Decimal("222.5")
        assert row.provider == "yfinance"
        assert row.market_code == "tw_equity"


async def test_empty_input_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as database:
        assert await _store(database, []) == 0
    async with session_factory() as database:
        assert await database.scalar(select(func.count()).select_from(IndexDailyBar)) == 0


async def test_nonpositive_close_is_rejected_by_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(IntegrityError):
        async with session_factory.begin() as database:
            await _store(database, [_bar(date(2026, 9, 1), "0")])


async def test_unknown_market_code_is_rejected_by_the_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bar = _bar(date(2026, 9, 1), "100.0").model_copy(update={"market": "not_a_market"})
    with pytest.raises(IntegrityError):
        async with session_factory.begin() as database:
            await _store(database, [bar])


class _StubAdapter:
    """Returns a canned result per symbol so a write failure can be provoked."""

    def __init__(self, results: dict[str, DailyBarsResult]) -> None:
        self._results = results

    async def get_daily_bars(
        self,
        *,
        market: str,
        symbol: str,
        period: str = "2y",
    ) -> DailyBarsResult:
        return self._results[symbol]


def _result(symbol: str, market: str, bars: tuple[DailyBar, ...]) -> DailyBarsResult:
    return DailyBarsResult(
        symbol=symbol,
        market=market,  # type: ignore[arg-type]
        items=bars,
        dropped_unsettled_trade_date=None,
        provenance=Provenance(
            provider="yfinance",
            contract_version="2026-09-03.v1",
            contract_hash="0" * 64,
            endpoint="Ticker.history",
            query_fingerprint="0" * 64,
            fetched_at=FETCHED_AT,
            as_of=bars[-1].trade_date,
            response_digest="0" * 64,
            record_count=len(bars),
        ),
    )


def _symbol_bar(symbol: str, market: str, close: str) -> DailyBar:
    return _bar(date(2026, 9, 1), close).model_copy(
        update={"symbol": symbol, "instrument_source_id": symbol, "market": market}
    )


async def test_a_failing_symbol_does_not_roll_back_the_others(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # ^HSI carries an unknown market code, so its insert violates the foreign
    # key. It sits between two healthy symbols to prove the batch neither loses
    # what came before it nor stops writing what comes after.
    results = {
        "^DJI": _result("^DJI", "us_equity", (_symbol_bar("^DJI", "us_equity", "100.0"),)),
        "^HSI": _result("^HSI", "hk_equity", (_symbol_bar("^HSI", "not_a_market", "200.0"),)),
        "^TWII": _result("^TWII", "tw_equity", (_symbol_bar("^TWII", "tw_equity", "300.0"),)),
    }
    adapter = cast(YfinanceAdapter, _StubAdapter(results))

    async with session_factory.begin() as database:
        refreshed, failures = await refresh_index_daily_bars(
            database,
            adapter=adapter,
            symbols=["^DJI", "^HSI", "^TWII"],
            period="7d",
        )

    assert [entry.result.symbol for entry in refreshed] == ["^DJI", "^TWII"]
    assert [entry.symbol for entry in failures] == ["^HSI"]
    assert "IntegrityError" in failures[0].error

    async with session_factory() as database:
        stored = (await database.scalars(select(IndexDailyBar.symbol))).all()
        assert sorted(stored) == ["^DJI", "^TWII"]
