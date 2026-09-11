import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from conftest import remigrate_database, reset_database_schema
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_health import readiness

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.modules.data_sources.api import (
    TRACKED_INDICES,
    DailyBar,
    IndexSymbol,
    Provenance,
    YfinanceAdapter,
    YfinanceDailyBars,
)
from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.modules.markets.api import (
    latest_index_bars,
    refresh_index_daily_bars,
    store_index_daily_bars,
)
from daily_insights_api.modules.markets.models import (
    IndexDailyBar,
    IndexDailyBarSeries,
    OrganizationMarketPolicy,
)
from daily_insights_api.modules.markets.service import (
    AUTOMATIC_SHORT_REFRESH_PERIOD,
    INITIAL_SERIES_BACKFILL_PERIOD,
    MAX_BIND_PARAMETERS,
    MAX_FETCH_CONCURRENCY,
    TAIEX_INCREMENTAL_MONTHS,
    TAIEX_INITIAL_BACKFILL_MONTHS,
    YFINANCE_INDICES,
    IndexProviderConflictError,
    select_taiex_refresh_months,
)
from daily_insights_api.modules.tenancy.models import Organization
from daily_insights_api.web.app import create_app

pytestmark = pytest.mark.integration

FETCHED_AT = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)


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


def _taipei_today() -> date:
    """The same day the endpoint's default window ends on.

    Using the machine's local date here instead would fail on any host running
    ahead of Taipei, where a row dated "today" lands after the window closes.
    """
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


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


async def _store_as(
    database: AsyncSession,
    bars: list[DailyBar],
    provider: str,
) -> int:
    return await store_index_daily_bars(
        database,
        bars=bars,
        provider=provider,
        contract_version="test",
        source_fetched_at=FETCHED_AT,
    )


async def _insert_raw_bar(
    database: AsyncSession,
    *,
    symbol: str,
    market_code: str,
    close: str,
) -> None:
    """Seed a legacy/corrupt valid-market row that bypasses application writes."""
    database.add(IndexDailyBarSeries(symbol=symbol, provider="yfinance"))
    await database.flush()
    database.add(
        IndexDailyBar(
            symbol=symbol,
            trade_date=date(2026, 9, 1),
            market_code=market_code,
            open=Decimal("100.0"),
            high=Decimal("101.0"),
            low=Decimal("99.0"),
            close=Decimal(close),
            volume=1_000,
            provider="yfinance",
            contract_version="test",
            source_fetched_at=FETCHED_AT,
        )
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


async def test_malformed_market_code_is_rejected_before_database_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bar = _bar(date(2026, 9, 1), "100.0").model_copy(update={"market": "not_a_market"})
    with pytest.raises(ValueError, match="does not match its catalog market"):
        async with session_factory.begin() as database:
            await _store(database, [bar])


async def test_store_rejects_a_catalog_market_mismatch_before_writing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    valid_but_wrong_market = _bar(date(2026, 9, 1), "100.0").model_copy(
        update={"market": "us_equity"}
    )
    malformed_market = _bar(date(2026, 9, 2), "200.0").model_copy(update={"market": "not_a_market"})

    async with session_factory.begin() as database:
        with pytest.raises(ValueError, match="does not match its catalog market"):
            await _store(database, [valid_but_wrong_market])
        with pytest.raises(ValueError, match="does not match its catalog market"):
            await _store(database, [malformed_market])

    async with session_factory() as database:
        assert await database.scalar(select(func.count()).select_from(IndexDailyBar)) == 0
        assert await database.scalar(select(func.count()).select_from(IndexDailyBarSeries)) == 0


class _StubAdapter:
    """Returns a canned result per symbol so a write failure can be provoked."""

    def __init__(self, results: dict[str, YfinanceDailyBars]) -> None:
        self._results = results

    async def get_daily_bars(
        self,
        *,
        market: str,
        symbol: str,
        period: str = "2y",
    ) -> YfinanceDailyBars:
        return self._results[symbol]


class _RecordingAdapter(_StubAdapter):
    """Captures provider windows while returning deterministic settled bars."""

    def __init__(self, results: dict[str, YfinanceDailyBars]) -> None:
        super().__init__(results)
        self.periods: list[tuple[str, str]] = []

    async def get_daily_bars(
        self,
        *,
        market: str,
        symbol: str,
        period: str = "2y",
    ) -> YfinanceDailyBars:
        self.periods.append((symbol, period))
        return await super().get_daily_bars(market=market, symbol=symbol, period=period)


def _result(symbol: str, market: str, bars: tuple[DailyBar, ...]) -> YfinanceDailyBars:
    return YfinanceDailyBars(
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


async def test_first_short_refresh_backfills_a_new_symbol_then_returns_to_short_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = cast(
        YfinanceAdapter,
        _RecordingAdapter(
            {"^NDX": _result("^NDX", "us_equity", (_symbol_bar("^NDX", "us_equity", "100.0"),))}
        ),
    )

    async with session_factory.begin() as database:
        await refresh_index_daily_bars(
            database, adapter=adapter, symbols=["^NDX"], period=AUTOMATIC_SHORT_REFRESH_PERIOD
        )
    assert cast(_RecordingAdapter, adapter).periods == [("^NDX", INITIAL_SERIES_BACKFILL_PERIOD)]

    async with session_factory.begin() as database:
        await refresh_index_daily_bars(
            database, adapter=adapter, symbols=["^NDX"], period=AUTOMATIC_SHORT_REFRESH_PERIOD
        )
    assert cast(_RecordingAdapter, adapter).periods == [
        ("^NDX", INITIAL_SERIES_BACKFILL_PERIOD),
        ("^NDX", AUTOMATIC_SHORT_REFRESH_PERIOD),
    ]


async def test_a_failing_symbol_does_not_roll_back_the_others(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # ^HSI carries an unknown market code. It sits between two healthy symbols
    # to prove the batch neither loses what came before it nor stops writing
    # what comes after.
    results = {
        "^DJI": _result("^DJI", "us_equity", (_symbol_bar("^DJI", "us_equity", "100.0"),)),
        "^HSI": _result("^HSI", "hk_equity", (_symbol_bar("^HSI", "not_a_market", "200.0"),)),
        "000001.SS": _result(
            "000001.SS", "cn_equity", (_symbol_bar("000001.SS", "cn_equity", "300.0"),)
        ),
    }
    adapter = cast(YfinanceAdapter, _StubAdapter(results))

    async with session_factory.begin() as database:
        refreshed, failures = await refresh_index_daily_bars(
            database,
            adapter=adapter,
            symbols=["^DJI", "^HSI", "000001.SS"],
            period="7d",
        )

    assert [entry.result.symbol for entry in refreshed] == ["^DJI", "000001.SS"]
    assert [entry.symbol for entry in failures] == ["^HSI"]
    assert "ValueError" in failures[0].error

    async with session_factory() as database:
        stored = (await database.scalars(select(IndexDailyBar.symbol))).all()
        assert sorted(stored) == ["000001.SS", "^DJI"]


async def test_a_backfill_larger_than_the_bind_parameter_limit_is_chunked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # `period=max` returns 24k+ rows for ^GSPC. A single INSERT binds one
    # parameter per column per row and PostgreSQL caps a statement at 65535, so
    # anything past MAX_BIND_PARAMETERS // columns rows must be split.
    columns = 11
    row_count = (MAX_BIND_PARAMETERS // columns) + 500
    start = date(1927, 12, 30)
    bars = [_bar(start + timedelta(days=offset), "100.0") for offset in range(row_count)]

    async with session_factory.begin() as database:
        stored = await _store(database, bars)
    assert stored == row_count

    async with session_factory() as database:
        assert (await database.scalar(select(func.count()).select_from(IndexDailyBar))) == row_count


class _SlowStubAdapter:
    """Records overlap so concurrent fetching can be observed."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self.in_flight = 0
        self.peak_in_flight = 0

    async def get_daily_bars(
        self,
        *,
        market: str,
        symbol: IndexSymbol,
        period: str = "2y",
    ) -> YfinanceDailyBars:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._delay)
        finally:
            self.in_flight -= 1
        return _result(
            symbol,
            TRACKED_INDICES[symbol],
            (_symbol_bar(symbol, TRACKED_INDICES[symbol], "100.0"),),
        )


async def test_symbols_are_fetched_concurrently_and_written_in_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Ten symbols in series can outlast the proxy budget when Yahoo is slow, so
    # the fetches overlap. The writes must stay sequential: an AsyncSession is
    # not safe for concurrent use.
    stub = _SlowStubAdapter(delay=0.05)
    symbols = list(YFINANCE_INDICES)

    async with session_factory.begin() as database:
        refreshed, failures = await refresh_index_daily_bars(
            database,
            adapter=cast(YfinanceAdapter, stub),
            symbols=symbols,
            period="7d",
        )

    assert failures == []
    assert stub.peak_in_flight == MAX_FETCH_CONCURRENCY
    assert [entry.result.symbol for entry in refreshed] == symbols


async def test_a_second_provider_cannot_overwrite_an_existing_series(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # docs/architecture/twelve-data-three-market-morning-report-plan.md forbids
    # splicing one provider's history onto another's. Without the guard the
    # upsert would do exactly that, one day at a time and without a trace.
    async with session_factory.begin() as database:
        await _store(database, [_bar(date(2026, 9, 1), "100.0")])

    with pytest.raises(ValueError, match="series belongs to provider 'yfinance'"):
        async with session_factory.begin() as database:
            await store_index_daily_bars(
                database,
                bars=[_bar(date(2026, 9, 1), "999.0")],
                provider="twelve_data",
                contract_version="other",
                source_fetched_at=FETCHED_AT,
            )

    async with session_factory() as database:
        row = await database.scalar(select(IndexDailyBar))
        assert row is not None
        assert row.provider == "yfinance"
        assert row.close == Decimal("100.0")


async def test_a_second_provider_cannot_append_a_nonoverlapping_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as database:
        await _store(database, [_bar(date(2026, 9, 1), "100.0")])

    with pytest.raises(ValueError, match="series belongs to provider 'yfinance'"):
        async with session_factory.begin() as database:
            await _store_as(
                database,
                [_bar(date(2026, 9, 2), "200.0")],
                "twelve_data",
            )

    async with session_factory() as database:
        rows = (await database.scalars(select(IndexDailyBar))).all()
        assert [(row.trade_date, row.provider) for row in rows] == [(date(2026, 9, 1), "yfinance")]


async def test_provider_can_switch_after_its_old_rows_are_deleted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as database:
        await _store(database, [_bar(date(2026, 9, 1), "100.0")])
        await database.execute(delete(IndexDailyBar).where(IndexDailyBar.symbol == "^TWII"))
        await _store_as(database, [_bar(date(2026, 9, 2), "200.0")], "twelve_data")

    async with session_factory() as database:
        series = await database.get(IndexDailyBarSeries, "^TWII")
        row = await database.scalar(select(IndexDailyBar))
        assert series is not None
        assert series.provider == "twelve_data"
        assert row is not None
        assert row.provider == "twelve_data"


async def test_provider_conflict_stays_isolated_to_one_refresh_symbol(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as database:
        await _store_as(
            database,
            [_symbol_bar("^HSI", "hk_equity", "50.0")],
            "twelve_data",
        )

    results = {
        "^DJI": _result("^DJI", "us_equity", (_symbol_bar("^DJI", "us_equity", "100.0"),)),
        "^HSI": _result("^HSI", "hk_equity", (_symbol_bar("^HSI", "hk_equity", "200.0"),)),
        "000001.SS": _result(
            "000001.SS", "cn_equity", (_symbol_bar("000001.SS", "cn_equity", "300.0"),)
        ),
    }
    async with session_factory.begin() as database:
        refreshed, failures = await refresh_index_daily_bars(
            database,
            adapter=cast(YfinanceAdapter, _StubAdapter(results)),
            symbols=["^DJI", "^HSI", "000001.SS"],
            period="7d",
        )

    assert [entry.result.symbol for entry in refreshed] == ["^DJI", "000001.SS"]
    assert [entry.symbol for entry in failures] == ["^HSI"]
    assert "IndexProviderConflictError" in failures[0].error
    async with session_factory() as database:
        stored = (await database.scalars(select(IndexDailyBar.symbol))).all()
        assert sorted(stored) == ["000001.SS", "^DJI", "^HSI"]


async def test_concurrent_different_provider_claims_serialize(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = session_factory()
    first_transaction = await first.begin()
    try:
        await _store(first, [_bar(date(2026, 9, 1), "100.0")])

        async def competing_write() -> None:
            with pytest.raises(ValueError, match="series belongs to provider 'yfinance'"):
                async with session_factory.begin() as database:
                    await _store_as(
                        database,
                        [_bar(date(2026, 9, 2), "200.0")],
                        "twelve_data",
                    )

        competitor = asyncio.create_task(competing_write())
        await asyncio.sleep(0.1)
        assert not competitor.done()
        await first_transaction.commit()
        await asyncio.wait_for(competitor, timeout=5)
    finally:
        if first_transaction.is_active:
            await first_transaction.rollback()
        await first.close()


async def test_reversed_multi_symbol_claims_take_locks_in_the_same_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Force opposite callers to pause before their first input symbol. Without
    # store_index_daily_bars sorting both claim lists, each transaction acquires
    # one symbol and waits on the other until PostgreSQL detects a deadlock.
    async with session_factory.begin() as database:
        await database.execute(
            text(
                """
                CREATE FUNCTION delay_reversed_index_claims() RETURNS trigger AS $$
                BEGIN
                  IF (NEW.provider = 'yfinance' AND NEW.symbol = '^DJI')
                     OR (NEW.provider = 'twelve_data' AND NEW.symbol = '^HSI') THEN
                    PERFORM pg_sleep(0.2);
                  END IF;
                  RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        await database.execute(
            text(
                """
                CREATE TRIGGER delay_reversed_index_claims
                BEFORE INSERT ON index_daily_bar_series
                FOR EACH ROW EXECUTE FUNCTION delay_reversed_index_claims()
                """
            )
        )

    async def write(provider: str, bars: list[DailyBar]) -> int | IndexProviderConflictError:
        try:
            async with session_factory.begin() as database:
                return await _store_as(database, bars, provider)
        except IndexProviderConflictError as error:
            return error

    dji = _symbol_bar("^DJI", "us_equity", "100.0")
    hsi = _symbol_bar("^HSI", "hk_equity", "200.0")
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            write("yfinance", [dji, hsi]),
            write("twelve_data", [hsi, dji]),
        ),
        timeout=5,
    )

    assert sum(isinstance(outcome, int) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, IndexProviderConflictError) for outcome in outcomes) == 1


def _signed_in_client(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    role: SystemRole,
    organization_id: uuid.UUID | None,
) -> AsyncClient:
    app = create_app(Settings(environment="test"), readiness(True), session_factory)

    async def auth() -> AuthContext:
        # A real AuthContext rather than a namespace behind a cast. The cast
        # silenced the type checker, so a new field on the dataclass, or a route
        # reaching for one this never set, would have surfaced as an
        # AttributeError mid-request instead of a type error here. The ORM
        # instances are never persisted; only these attributes are read.
        return AuthContext(
            user=User(system_role=role),
            session=Session(),
            organization_id=organization_id,
        )

    app.dependency_overrides[require_password_changed] = auth
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def member_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """An authenticated organization member with every market visible."""
    async with _signed_in_client(
        session_factory, role=SystemRole.ORG_MEMBER, organization_id=uuid.uuid4()
    ) as client:
        yield client


async def test_internal_staff_read_indices_without_an_organization(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Internal users belong to no organization, so a membership check alone
    # would lock them out. reports/access.py grants them the same preview.
    #
    # The request below carries no date range, so it gets the default window of
    # the last 365 days. The row is dated today rather than with this file's
    # fixed 2026-09-01 so that the window keeps containing it; a fixed date here
    # would leave the assertion passing until it silently aged out of range.
    async with session_factory.begin() as database:
        await _store(
            database,
            [
                _symbol_bar("^DJI", "us_equity", "500.0").model_copy(
                    update={"trade_date": _taipei_today()}
                )
            ],
        )

    async with _signed_in_client(
        session_factory, role=SystemRole.ADMIN, organization_id=None
    ) as admin_client:
        listed = await admin_client.get("/api/markets/indices")
        bars = await admin_client.get("/api/markets/indices/%5EDJI/daily-bars")
        moving_averages = await admin_client.get("/api/markets/indices/%5EDJI/moving-averages")

    assert listed.status_code == 200, listed.text
    assert [item["symbol"] for item in listed.json()] == ["^DJI"]
    assert bars.status_code == 200, bars.text
    assert [bar["close"] for bar in bars.json()] == ["500.0000000000"]
    assert moving_averages.status_code == 200, moving_averages.text
    assert moving_averages.json()["symbol"] == "^DJI"


async def test_a_member_without_an_organization_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _signed_in_client(
        session_factory, role=SystemRole.ORG_MEMBER, organization_id=None
    ) as client:
        response = await client.get("/api/markets/indices")
    assert response.status_code == 403
    assert response.json()["detail"] == "organization membership required"


async def test_latest_bars_carry_the_previous_close_and_respect_visibility(
    session_factory: async_sessionmaker[AsyncSession],
    member_client: AsyncClient,
) -> None:
    async with session_factory.begin() as database:
        await _store(
            database,
            [
                _bar(date(2026, 9, 1), "100.0"),
                _bar(date(2026, 9, 2), "110.0"),
                _symbol_bar("^DJI", "us_equity", "500.0"),
                _symbol_bar("^HSI", "hk_equity", "700.0"),
            ],
        )

    response = await member_client.get("/api/markets/indices")
    assert response.status_code == 200, response.text
    summary = [(item["symbol"], item["close"], item["previous_close"]) for item in response.json()]
    assert summary == [
        ("^DJI", "500.0000000000", None),
        ("^HSI", "700.0000000000", None),
        ("^TWII", "110.0000000000", "100.0000000000"),
    ]

    async with session_factory() as database:
        hk_only = await latest_index_bars(database, market_codes={"hk_equity"})
    assert [item.symbol for item in hk_only] == ["^HSI"]


async def test_catalog_market_filters_reject_mismatched_legacy_rows_from_tenant_reads(
    session_factory: async_sessionmaker[AsyncSession],
    member_client: AsyncClient,
) -> None:
    # These market codes are valid and the rows satisfy every database
    # constraint. They model a row written before application-side catalog
    # validation and must not leak through either reader.
    async with session_factory.begin() as database:
        await _insert_raw_bar(database, symbol="^TWII", market_code="us_equity", close="300.0")
        await _insert_raw_bar(database, symbol="^HSI", market_code="tw_equity", close="700.0")

    latest = await member_client.get("/api/markets/indices")
    tw_history = await member_client.get("/api/markets/indices/%5ETWII/daily-bars")
    hk_history = await member_client.get("/api/markets/indices/%5EHSI/daily-bars")
    tw_moving_averages = await member_client.get("/api/markets/indices/%5ETWII/moving-averages")
    hk_moving_averages = await member_client.get("/api/markets/indices/%5EHSI/moving-averages")

    assert latest.status_code == 200, latest.text
    assert latest.json() == []
    assert tw_history.status_code == 200, tw_history.text
    assert tw_history.json() == []
    assert hk_history.status_code == 200, hk_history.text
    assert hk_history.json() == []
    assert tw_moving_averages.status_code == 200, tw_moving_averages.text
    assert tw_moving_averages.json()["as_of"] is None
    assert hk_moving_averages.status_code == 200, hk_moving_averages.text
    assert hk_moving_averages.json()["as_of"] is None


async def test_a_zero_price_is_served_as_fixed_point_not_an_exponent(
    session_factory: async_sessionmaker[AsyncSession],
    member_client: AsyncClient,
) -> None:
    # Numeric(20,10) returns Decimal("0E-10") for a stored zero. Serialized as
    # that exponent form it breaks the pattern this API declares for decimals,
    # and one such row would fail a client's parse of the entire list. Yahoo
    # does return Open=0 on old rows, and only close is constrained upstream.
    zero_open = _bar(date(2026, 9, 1), "100.5").model_copy(
        update={"open": Decimal("0"), "high": Decimal("0"), "low": Decimal("0")}
    )
    async with session_factory.begin() as database:
        await _store(database, [zero_open])

    response = await member_client.get(
        "/api/markets/indices/%5ETWII/daily-bars",
        params={"start": "2026-08-01", "end": "2026-09-30"},
    )
    assert response.status_code == 200, response.text
    bar = response.json()[0]
    assert bar["open"] == "0.0000000000"
    assert bar["high"] == "0.0000000000"
    assert bar["close"] == "100.5000000000"

    latest = await member_client.get("/api/markets/indices")
    assert latest.json()[0]["open"] == "0.0000000000"

    decimal_string = re.compile(r"^-?\d+(?:\.\d+)?$")
    for field in ("open", "high", "low", "close"):
        assert decimal_string.match(bar[field]), f"{field}={bar[field]!r} is not a decimal string"


async def test_daily_bars_are_bounded_by_the_requested_window(
    session_factory: async_sessionmaker[AsyncSession],
    member_client: AsyncClient,
) -> None:
    today = _taipei_today()
    async with session_factory.begin() as database:
        await _store(
            database,
            [
                _bar(today - timedelta(days=400), "1.0"),
                _bar(today - timedelta(days=10), "2.0"),
                _bar(today - timedelta(days=1), "3.0"),
            ],
        )

    default_window = await member_client.get("/api/markets/indices/%5ETWII/daily-bars")
    assert default_window.status_code == 200, default_window.text
    assert [bar["close"] for bar in default_window.json()] == ["2.0000000000", "3.0000000000"]

    explicit = await member_client.get(
        "/api/markets/indices/%5ETWII/daily-bars",
        params={
            "start": (today - timedelta(days=500)).isoformat(),
            "end": (today - timedelta(days=5)).isoformat(),
        },
    )
    assert [bar["close"] for bar in explicit.json()] == ["1.0000000000", "2.0000000000"]

    inverted = await member_client.get(
        "/api/markets/indices/%5ETWII/daily-bars",
        params={"start": today.isoformat(), "end": (today - timedelta(days=1)).isoformat()},
    )
    assert inverted.status_code == 422

    too_wide = await member_client.get(
        "/api/markets/indices/%5ETWII/daily-bars",
        params={"start": "1927-12-30", "end": today.isoformat()},
    )
    assert too_wide.status_code == 422
    assert too_wide.json()["detail"] == "date range must not exceed 10 years"

    unknown = await member_client.get("/api/markets/indices/NOPE/daily-bars")
    assert unknown.status_code == 404


async def test_moving_averages_use_hidden_warmup_and_only_expose_requested_dates(
    session_factory: async_sessionmaker[AsyncSession],
    member_client: AsyncClient,
) -> None:
    start = date(2026, 1, 20)
    async with session_factory.begin() as database:
        await _store(
            database,
            [
                *[_bar(start - timedelta(days=offset), "1") for offset in range(1, 20)],
                _bar(start, "2"),
                _bar(start + timedelta(days=3), "3"),
            ],
        )

    response = await member_client.get(
        "/api/markets/indices/%5ETWII/moving-averages",
        params={"start": start.isoformat(), "end": (start + timedelta(days=3)).isoformat()},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["method"] == "sma"
    assert payload["price_field"] == "close"
    assert payload["formula_version"] == "sma-close-v1"
    assert payload["as_of"] == "2026-01-23"
    assert [item["period"] for item in payload["series"]] == [20, 60, 120, 240]
    assert payload["series"][0]["points"] == [
        {"trade_date": "2026-01-20", "value": "1.0500000000"},
        {"trade_date": "2026-01-23", "value": "1.1500000000"},
    ]
    assert payload["series"][1]["points"] == [
        {"trade_date": "2026-01-20", "value": None},
        {"trade_date": "2026-01-23", "value": None},
    ]


async def test_moving_average_route_has_daily_bar_visibility_and_range_contract(
    session_factory: async_sessionmaker[AsyncSession],
    member_client: AsyncClient,
) -> None:
    async with session_factory.begin() as database:
        await _store(database, [_bar(date(2026, 9, 1), "100")])

    no_data = await member_client.get(
        "/api/markets/indices/%5ETWII/moving-averages",
        params={"start": "2026-08-01", "end": "2026-08-31"},
    )
    assert no_data.status_code == 200, no_data.text
    assert no_data.json()["as_of"] is None
    assert [item["points"] for item in no_data.json()["series"]] == [[], [], [], []]

    unknown = await member_client.get("/api/markets/indices/NOPE/moving-averages")
    inverted = await member_client.get(
        "/api/markets/indices/%5ETWII/moving-averages",
        params={"start": "2026-09-02", "end": "2026-09-01"},
    )
    assert unknown.status_code == 404
    assert inverted.status_code == 422


async def test_a_hidden_market_is_absent_from_the_list_and_404_on_its_route(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Exercised through the router on purpose. Asserting that the service
    # filters by the codes it is handed says nothing about whether the route
    # still works out which codes those are, so this reaches for a real policy
    # row and goes over HTTP.
    hidden_from = uuid.uuid4()
    sees_everything = uuid.uuid4()
    author_id = uuid.uuid4()
    today = _taipei_today()
    async with session_factory.begin() as database:
        database.add_all(
            [
                Organization(id=hidden_from, name="Contract A", slug="contract-a", seat_limit=1),
                Organization(
                    id=sees_everything, name="Contract B", slug="contract-b", seat_limit=1
                ),
                User(
                    id=author_id,
                    email="policy-author@example.com",
                    display_name="Policy Author",
                    # This test never authenticates; the column is only NOT NULL.
                    password_hash="unused",
                    must_change_password=False,
                    system_role=SystemRole.ADMIN,
                    status=UserStatus.ACTIVE,
                ),
            ]
        )
        await database.flush()
        database.add(
            OrganizationMarketPolicy(
                organization_id=hidden_from,
                market_code="hk_equity",
                is_visible=False,
                changed_by_user_id=author_id,
                changed_at=datetime.now(UTC),
            )
        )
        await _store(
            database,
            [
                _bar(today, "300.0"),
                _symbol_bar("^HSI", "hk_equity", "700.0").model_copy(update={"trade_date": today}),
            ],
        )

    async with _signed_in_client(
        session_factory, role=SystemRole.ORG_MEMBER, organization_id=hidden_from
    ) as restricted:
        listed = await restricted.get("/api/markets/indices")
        hidden_route = await restricted.get("/api/markets/indices/%5EHSI/daily-bars")
        hidden_moving_averages = await restricted.get("/api/markets/indices/%5EHSI/moving-averages")
        allowed_route = await restricted.get("/api/markets/indices/%5ETWII/daily-bars")
    async with _signed_in_client(
        session_factory, role=SystemRole.ORG_MEMBER, organization_id=sees_everything
    ) as unrestricted:
        listed_elsewhere = await unrestricted.get("/api/markets/indices")

    # The row exists; only the contract removes it. Without this the assertions
    # below would pass just as well against an empty table.
    assert "^HSI" in [item["symbol"] for item in listed_elsewhere.json()]

    assert listed.status_code == 200, listed.text
    assert [item["symbol"] for item in listed.json()] == ["^TWII"]
    assert hidden_route.status_code == 404
    assert hidden_route.json()["detail"] == "index not found"
    assert hidden_moving_averages.status_code == 404
    assert hidden_moving_averages.json()["detail"] == "index not found"
    assert allowed_route.status_code == 200, allowed_route.text
    assert [bar["close"] for bar in allowed_route.json()] == ["300.0000000000"]


async def test_every_listed_index_is_reachable_on_its_own_route(
    session_factory: async_sessionmaker[AsyncSession],
    member_client: AsyncClient,
) -> None:
    # The two endpoints must agree on which symbols exist. Nothing deletes
    # history, so a symbol dropped from the catalog keeps its rows; if the list
    # were driven by what the table holds while the detail route asked the
    # catalog, the list would offer a link that answers 404.
    today = _taipei_today()
    async with session_factory.begin() as database:
        await _store(database, [_bar(today, "300.0"), _symbol_bar("^DJI", "us_equity", "500.0")])
        await _insert_raw_bar(database, symbol="^RETIRED", market_code="tw_equity", close="100.0")

    listed = await member_client.get("/api/markets/indices")
    assert listed.status_code == 200, listed.text
    symbols = [item["symbol"] for item in listed.json()]
    assert symbols, "the list must not be empty or the loop below proves nothing"
    assert "^RETIRED" not in symbols

    for symbol in symbols:
        detail = await member_client.get(
            f"/api/markets/indices/{quote(symbol, safe='')}/daily-bars"
        )
        assert detail.status_code == 200, f"{symbol} is listed but its route answers {detail.text}"

    orphan = await member_client.get("/api/markets/indices/%5ERETIRED/daily-bars")
    assert orphan.status_code == 404


async def test_exactly_ten_calendar_years_is_accepted_and_a_day_more_is_not(
    session_factory: async_sessionmaker[AsyncSession],
    member_client: AsyncClient,
) -> None:
    # The limit is stated to callers in years, so it has to be counted in
    # years. A decade spans 3,652 or 3,653 days depending on its leap days,
    # so a 3,650-day cap would refuse the very range the message offers.
    async def range_status(start: str, end: str) -> int:
        response = await member_client.get(
            "/api/markets/indices/%5ETWII/daily-bars",
            params={"start": start, "end": end},
        )
        return response.status_code

    # 2016-01-01 to 2026-01-01 carries three leap days: 3,653 apart.
    assert await range_status("2016-01-01", "2026-01-01") == 200
    assert await range_status("2015-12-31", "2026-01-01") == 422
    # A leap day has no counterpart ten common years earlier.
    assert await range_status("2018-02-28", "2028-02-29") == 200
    assert await range_status("2018-02-27", "2028-02-29") == 422


async def test_an_end_at_the_start_of_the_calendar_is_answered_not_crashed(
    session_factory: async_sessionmaker[AsyncSession],
    member_client: AsyncClient,
) -> None:
    # Deriving the default start subtracts a year from `end`, which underflows
    # for any date in year 1 and used to surface as a 500. The whole year is
    # covered, not just date.min, because the window is 365 days wide.
    for end in ("0001-01-01", "0001-06-15", "0001-12-31"):
        response = await member_client.get(
            "/api/markets/indices/%5ETWII/daily-bars", params={"end": end}
        )
        assert response.status_code == 200, f"end={end}: {response.text}"
        assert response.json() == []


async def test_the_same_provider_still_updates_an_existing_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as database:
        await _store(database, [_bar(date(2026, 9, 1), "100.0")])
    async with session_factory.begin() as database:
        assert await _store(database, [_bar(date(2026, 9, 1), "123.5")]) == 1

    async with session_factory() as database:
        row = await database.scalar(select(IndexDailyBar))
        assert row is not None
        assert row.close == Decimal("123.5")


async def test_an_empty_taiex_series_widens_the_incremental_window_to_the_backfill(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The provider switch deletes ^TWII's rows and backfills nothing.

    Without this the refresh would only ever ask for its incremental window and
    the two years behind it would never come back. Mirrors the Yahoo path's
    two-year bootstrap for a series with no stored bars.
    """
    today = date(2026, 9, 11)

    async with session_factory() as database:
        empty = await select_taiex_refresh_months(
            database, today=today, requested_months=TAIEX_INCREMENTAL_MONTHS
        )
    assert len(empty) == TAIEX_INITIAL_BACKFILL_MONTHS
    assert empty[-1] == date(2026, 9, 1)
    assert empty[0] == date(2024, 9, 1)

    async with session_factory.begin() as database:
        await _store_as(database, [_bar(date(2026, 9, 1), "100.0")], "twse")

    async with session_factory() as database:
        populated = await select_taiex_refresh_months(
            database, today=today, requested_months=TAIEX_INCREMENTAL_MONTHS
        )
    assert list(populated) == [date(2026, 8, 1), date(2026, 9, 1)]


async def test_an_explicit_taiex_window_is_never_widened(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A CLI backfill asking for three months means three, even on an empty
    # series: only the incremental default carries the bootstrap.
    async with session_factory() as database:
        months = await select_taiex_refresh_months(
            database, today=date(2026, 9, 11), requested_months=3
        )
    assert list(months) == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]
