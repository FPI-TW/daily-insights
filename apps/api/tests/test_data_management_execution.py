import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path as FileSystemPath
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from anyio import Path
from sqlalchemy.exc import IntegrityError

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.data_management.models import DataManagementRun
from daily_insights_api.modules.data_management.service import (
    RunAlreadyActiveError,
    cancel_run,
    enqueue_run,
    execute_run,
    worker_loop,
)
from daily_insights_api.modules.data_sources.api import DataSourceError
from daily_insights_api.modules.markets.api import (
    AUTOMATIC_SHORT_REFRESH_PERIOD,
    TAIEX_INCREMENTAL_MONTHS,
    TAIEX_SYMBOL,
    InstitutionalMarketFlow,
    TaiexRefresh,
)
from daily_insights_api.modules.reports.api import (
    LaunchMarketCode,
    MorningDatasetExecution,
    MorningMarketExecution,
)


def _run(operation: str, market_code: str | None = None) -> DataManagementRun:
    return DataManagementRun(
        id=uuid.uuid4(),
        operation=operation,
        market_code=market_code,
        edition_date=date(2026, 9, 7),
        status="running",
        requested_by_user_id=None,
    )


class _StubSessionFactory:
    """Stands in for `async_sessionmaker`: `begin()` yields a throwaway session."""

    def begin(self) -> "_StubSessionFactory":
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


class _NoopTransport:
    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> "_NoopTransport":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _NoopAdapter:
    def __init__(self, _: object) -> None:
        pass


class _DatabaseWithFlushFailure:
    def __init__(self, error: IntegrityError) -> None:
        self.error = error
        self.flush = AsyncMock(side_effect=error)
        self.rollback = AsyncMock()

    def add(self, _: object) -> None:
        pass


class _PostgresError(Exception):
    sqlstate = "23505"

    def __init__(self, constraint_name: str) -> None:
        self.diag = SimpleNamespace(constraint_name=constraint_name)


@pytest.mark.asyncio
async def test_enqueue_only_maps_named_active_run_unique_conflicts_to_409_error() -> None:
    active_error = IntegrityError(
        "INSERT", {}, _PostgresError("uq_data_management_runs_active_news")
    )
    active_database = _DatabaseWithFlushFailure(active_error)
    with pytest.raises(RunAlreadyActiveError):
        await enqueue_run(
            cast(Any, active_database),
            operation="news_market",
            market_code="global",
            requester_id=uuid.uuid4(),
            request_id=None,
        )
    active_database.rollback.assert_awaited_once()

    other_error = IntegrityError("INSERT", {}, _PostgresError("some_other_constraint"))
    other_database = _DatabaseWithFlushFailure(other_error)
    with pytest.raises(IntegrityError) as raised:
        await enqueue_run(
            cast(Any, other_database),
            operation="news_market",
            market_code="global",
            requester_id=uuid.uuid4(),
            request_id=None,
        )
    assert raised.value is other_error
    other_database.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_run_terminalizes_pending_or_running_work_without_a_lease() -> None:
    run = _run("macro_dashboard")

    class Database:
        async def scalar(self, _: object) -> DataManagementRun:
            return run

        def add(self, _: object) -> None:
            pass

        async def commit(self) -> None:
            pass

        async def rollback(self) -> None:
            raise AssertionError("a cancellable run must not be rolled back")

    cancelled = await cancel_run(
        cast(Any, Database()),
        run_id=run.id,
        actor_user_id=uuid.uuid4(),
        request_id="test-request",
    )
    assert cancelled is run
    assert run.status == "cancelled"
    assert run.lease_owner is None and run.lease_expires_at is None
    assert run.completed_at is not None
    assert run.result is not None and run.result["cancelled"] is True


@pytest.mark.asyncio
async def test_morning_execution_preserves_no_change_dataset_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.data_management import service

    execution = MorningMarketExecution(
        market_code="crypto",
        publication_action="no_change",
        revision=2,
        report_status="complete",
        source_date=date(2026, 9, 7),
        datasets=(
            MorningDatasetExecution(
                dataset_key="crypto_prices",
                status="succeeded",
                fetched_at=datetime(2026, 9, 7, tzinfo=UTC),
                source_as_of=date(2026, 9, 7),
                record_count=12,
                error=None,
            ),
        ),
    )

    async def execute(*_: object, **__: object) -> tuple[MorningMarketExecution, ...]:
        return (execution,)

    monkeypatch.setattr(service, "TwelveDataTransport", _NoopTransport)
    monkeypatch.setattr(service, "TwelveDataAdapter", _NoopAdapter)
    monkeypatch.setattr(service, "run_morning_report_edition", execute)
    status, result, error = await execute_run(
        _run("morning_market", "crypto"),
        cast(Any, None),
        Settings(environment="test", morning_reports_enabled=True, twelve_data_api_key="key"),
    )
    assert status == "succeeded" and error is None
    markets = cast(list[dict[str, object]], result["markets"])
    market = markets[0]
    assert market["publication_action"] == "no_change"
    assert market["revision"] == 2 and market["report_status"] == "complete"
    datasets = cast(list[dict[str, object]], market["datasets"])
    dataset = datasets[0]
    assert dataset == {
        "dataset_key": "crypto_prices",
        "status": "succeeded",
        "fetched_at": "2026-09-07T00:00:00+00:00",
        "source_as_of": "2026-09-07",
        "record_count": 12,
        "error": None,
    }


@pytest.mark.asyncio
async def test_morning_full_classifies_degraded_and_failed_markets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.data_management import service

    async def execute(*_: object, **kwargs: object) -> tuple[MorningMarketExecution, ...]:
        market = cast(tuple[str, ...], kwargs["market_codes"])[0]
        if market == "crypto":
            raise RuntimeError("token=secret")
        return (
            MorningMarketExecution(
                market_code=cast(LaunchMarketCode, market),
                publication_action="published",
                revision=1,
                report_status="partial",
                source_date=date(2026, 9, 7),
                datasets=(),
            ),
        )

    monkeypatch.setattr(service, "TwelveDataTransport", _NoopTransport)
    monkeypatch.setattr(service, "TwelveDataAdapter", _NoopAdapter)
    monkeypatch.setattr(service, "run_morning_report_edition", execute)
    status, result, error = await execute_run(
        _run("morning_all"),
        cast(Any, None),
        Settings(environment="test", morning_reports_enabled=True, twelve_data_api_key="key"),
    )
    assert status == "partial" and error == "runtimeerror"
    markets = cast(list[dict[str, object]], result["markets"])
    assert any(item["publication_action"] == "failed" for item in markets)
    assert any(item.get("report_status") == "partial" for item in markets)
    assert all("secret" not in str(item) for item in markets)

    async def all_fail(*_: object, **__: object) -> tuple[MorningMarketExecution, ...]:
        raise RuntimeError("api_key=secret")

    monkeypatch.setattr(service, "run_morning_report_edition", all_fail)
    failed_status, failed_result, failed_error = await execute_run(
        _run("morning_all"),
        cast(Any, None),
        Settings(environment="test", morning_reports_enabled=True, twelve_data_api_key="key"),
    )
    assert failed_status == "failed" and failed_error == "runtimeerror"
    failed_markets = cast(list[dict[str, object]], failed_result["markets"])
    assert all(item["publication_action"] == "failed" for item in failed_markets)


@pytest.mark.asyncio
async def test_yahoo_execution_uses_the_incremental_period_and_keeps_symbol_error_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.data_management import service

    calls: dict[str, object] = {}

    async def refresh(*_: object, **kwargs: object) -> tuple[list[object], list[object]]:
        calls.update(kwargs)
        return (
            [
                SimpleNamespace(
                    result=SimpleNamespace(
                        symbol="^DJI",
                        provenance=SimpleNamespace(
                            fetched_at=datetime(2026, 9, 7, tzinfo=UTC),
                            as_of=date(2026, 9, 6),
                        ),
                    ),
                    stored_count=7,
                )
            ],
            [SimpleNamespace(symbol="^HSI", error="api_key=secret")],
        )

    async def select_months(*_: object, **kwargs: object) -> tuple[date, ...]:
        calls["taiex_requested_months"] = kwargs["requested_months"]
        return (date(2026, 8, 1), date(2026, 9, 1))

    async def refresh_taiex(*_: object, **__: object) -> TaiexRefresh:
        return TaiexRefresh(
            stored_count=21,
            as_of=date(2026, 9, 10),
            fetched_at=datetime(2026, 9, 11, tzinfo=UTC),
        )

    monkeypatch.setattr(service, "YfinanceAdapter", lambda **_: object())
    monkeypatch.setattr(service, "refresh_index_daily_bars", refresh)
    monkeypatch.setattr(service, "select_taiex_refresh_months", select_months)
    monkeypatch.setattr(service, "refresh_taiex_daily_bars", refresh_taiex)
    status, result, error = await execute_run(
        _run("index_yahoo"),
        cast(Any, _StubSessionFactory()),
        Settings(environment="test", yfinance_enabled=True, twse_enabled=True),
    )
    assert status == "partial" and error == "index_symbol_failures"
    assert calls["period"] == AUTOMATIC_SHORT_REFRESH_PERIOD
    # ^TWII belongs to TWSE now, so it must not be in the Yahoo request.
    assert TAIEX_SYMBOL not in cast(list[str], calls["symbols"])
    symbols = cast(list[dict[str, object]], result["symbols"])
    assert symbols[0]["record_count"] == 7 and symbols[0]["source_as_of"] == "2026-09-06"
    taiex = next(item for item in symbols if item["symbol"] == TAIEX_SYMBOL)
    assert taiex["status"] == "succeeded" and taiex["record_count"] == 21
    # The run always asks for the incremental window; widening an empty series
    # to the two-year backfill is the selector's decision, not the run's.
    assert calls["taiex_requested_months"] == TAIEX_INCREMENTAL_MONTHS
    # The operator reads this string in the back office, so it keeps the cause
    # instead of collapsing to a class name -- with secrets still redacted.
    assert symbols[1]["error"] == "api_key=[REDACTED]"


@pytest.mark.asyncio
async def test_news_execution_routes_market_and_all_runs_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.data_management import service

    calls: list[str] = []

    class Client:
        def __init__(self, **_: object) -> None:
            pass

        async def aclose(self) -> None:
            calls.append("closed")

    async def market(*_: object, **kwargs: object) -> str:
        calls.append(f"market:{cast(Any, kwargs['spec']).market_code}")
        return "complete"

    async def all_editions(*_: object, **__: object) -> tuple[str, dict[str, str]]:
        calls.append("all")
        return "partial", {"global": "complete", "tw_equity": "partial", "us_equity": "failed"}

    monkeypatch.setattr(service, "create_news_client", lambda **_: Client())
    monkeypatch.setattr(service, "run_news_edition", market)
    monkeypatch.setattr(service, "run_all_editions_with_outcomes", all_editions)
    settings = Settings(environment="test", daily_news_enabled=True, news_model_api_key="key")
    market_status, market_result, market_error = await execute_run(
        _run("news_market", "tw_equity"), cast(Any, None), settings
    )
    all_status, all_result, all_error = await execute_run(
        _run("news_all"), cast(Any, None), settings
    )
    assert (market_status, market_error) == ("succeeded", None)
    assert (all_status, all_error) == ("partial", "news_partial")
    assert market_result == {"outcome": "complete", "outcomes": {"tw_equity": "complete"}}
    assert all_result == {
        "outcome": "partial",
        "outcomes": {"global": "complete", "tw_equity": "partial", "us_equity": "failed"},
    }
    assert calls == ["market:tw_equity", "closed", "all", "closed"]


@pytest.mark.asyncio
async def test_news_execution_rejects_disabled_or_missing_model_key() -> None:
    status, result, error = await execute_run(
        _run("news_all"),
        cast(Any, None),
        Settings(environment="test", daily_news_enabled=False),
    )
    assert (status, result, error) == (
        "failed",
        {
            "outcome": "unavailable",
            "outcomes": {
                "global": "unavailable",
                "tw_equity": "unavailable",
                "us_equity": "unavailable",
            },
        },
        "daily_news_unavailable",
    )


@pytest.mark.asyncio
async def test_active_worker_refreshes_database_lease_and_health_heartbeat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: FileSystemPath
) -> None:
    from daily_insights_api.modules.data_management import service

    run = _run("index_yahoo")
    heartbeat_path = Path(tmp_path / "data-management-heartbeat")
    heartbeat_seen = asyncio.Event()
    heartbeats: list[str] = []

    class ExecutionSession:
        def __call__(self) -> "ExecutionSession":
            return self

        async def __aenter__(self) -> "ExecutionSession":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, _: object) -> None:
            return None

        async def scalar(self, _: object) -> object:
            return run.id

        async def rollback(self) -> None:
            return None

    async def claim(_: object, __: str) -> object:
        return run

    async def heartbeat(_: object, __: object, ___: str) -> bool:
        heartbeats.append("lease")
        heartbeat_seen.set()
        return True

    async def execute(*_: object) -> tuple[str, dict[str, object], str | None]:
        await asyncio.wait_for(heartbeat_seen.wait(), timeout=1)
        return "succeeded", {}, None

    async def complete(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(service, "claim_next_run", claim)
    monkeypatch.setattr(service, "heartbeat_run", heartbeat)
    monkeypatch.setattr(service, "execute_run", execute)
    monkeypatch.setattr(service, "complete_run", complete)
    await worker_loop(
        cast(Any, ExecutionSession()),
        Settings(environment="test"),
        once=True,
        heartbeat_path=heartbeat_path,
        heartbeat_seconds=0.001,
    )
    assert heartbeats == ["lease"]
    assert await heartbeat_path.exists()


def _weekdays_before(day: date, count: int) -> set[date]:
    days: set[date] = set()
    cursor = day
    while len(days) < count:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            days.add(cursor)
    return days


@pytest.mark.asyncio
async def test_institutional_twse_rerun_whose_only_fetch_fails_is_partial_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-run stores nothing because every older date is already covered, so
    `stored_rows` cannot tell a healthy window from an empty one."""
    from daily_insights_api.modules.data_management import service

    edition = date(2026, 9, 7)
    stored_market = _weekdays_before(edition, 40)
    stored_stock = _weekdays_before(edition, 1)

    class Adapter:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "Adapter":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_stock_flows(self, trade_date: date) -> SimpleNamespace:
            if trade_date == edition:
                raise DataSourceError("boom")
            return SimpleNamespace(
                trade_date=trade_date, items=(), fetched_at=datetime(2026, 9, 7, 9, tzinfo=UTC)
            )

        get_market_flows = get_stock_flows

    async def existing(*_: object, **kwargs: object) -> set[date]:
        return stored_market if kwargs["flows"] is InstitutionalMarketFlow else stored_stock

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)

    run = _run("institutional_twse")
    run.edition_date = edition
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert (status, error) == ("partial", "twse_fetch_failures")
    assert cast(dict[str, Any], result["market_flows"])["covered_trading_days"] == 40
    assert cast(dict[str, Any], result["stock_flows"])["covered_trading_days"] == 1


@pytest.mark.asyncio
async def test_institutional_twse_stops_walking_once_the_source_is_clearly_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outage would otherwise cost 90 requests: 10 stock dates plus 80
    market dates, each spaced by the adapter's interval."""
    from daily_insights_api.modules.data_management import service

    asked: list[date] = []

    class Adapter:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "Adapter":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_stock_flows(self, trade_date: date) -> SimpleNamespace:
            asked.append(trade_date)
            raise DataSourceError("boom")

        get_market_flows = get_stock_flows

    async def existing(*_: object, **__: object) -> set[date]:
        return set()

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)

    run = _run("institutional_twse")
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert (status, error) == ("failed", "twse_fetch_failures")
    assert len(asked) == 2 * service.MAX_CONSECUTIVE_FAILURES
    for walk in ("stock_flows", "market_flows"):
        summary = cast(dict[str, Any], result[walk])
        assert summary["aborted"] is True
        assert len(cast(list[Any], summary["days"])) == service.MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_institutional_twse_reaching_no_trading_day_is_failed_not_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TWSE answers a date it cannot serve with HTTP 200 and a no-data stat, so
    an upstream outage raises nothing and stores nothing."""
    from daily_insights_api.modules.data_management import service

    class Adapter:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "Adapter":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_stock_flows(self, trade_date: date) -> SimpleNamespace:
            return SimpleNamespace(
                trade_date=trade_date, items=(), fetched_at=datetime(2026, 9, 7, 9, tzinfo=UTC)
            )

        get_market_flows = get_stock_flows

    async def existing(*_: object, **__: object) -> set[date]:
        return set()

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)

    run = _run("institutional_twse")
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert (status, error) == ("failed", "twse_no_coverage")
    assert cast(dict[str, Any], result["market_flows"])["covered_trading_days"] == 0


@pytest.mark.asyncio
async def test_institutional_twse_walks_back_to_forty_trading_days_without_refetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.data_management import service

    edition = date(2026, 9, 7)  # Monday
    # The five most recent weekdays are already stored: they must count toward
    # the window without a request.
    already_stored = {date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)}
    fetched_market: list[date] = []
    fetched_stock: list[date] = []
    stored: list[tuple[str, date, int]] = []

    def flows(trade_date: date, items: int) -> SimpleNamespace:
        return SimpleNamespace(
            trade_date=trade_date,
            items=tuple(range(items)),
            fetched_at=datetime(2026, 9, 7, 9, tzinfo=UTC),
        )

    class Adapter:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "Adapter":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_stock_flows(self, trade_date: date) -> SimpleNamespace:
            fetched_stock.append(trade_date)
            return flows(trade_date, 0 if trade_date.weekday() >= 5 else 10)

        async def get_market_flows(self, trade_date: date) -> SimpleNamespace:
            fetched_market.append(trade_date)
            if trade_date == date(2026, 8, 3):
                raise DataSourceError("boom")
            # Weekends have no trading.
            return flows(trade_date, 0 if trade_date.weekday() >= 5 else 5)

    async def existing(*_: object, **kwargs: object) -> set[date]:
        # Only the market table has history; the stock table starts empty.
        return already_stored if kwargs["flows"] is InstitutionalMarketFlow else set()

    async def store_market(_: object, *, market_code: str, flows: SimpleNamespace) -> int:
        stored.append((market_code, flows.trade_date, len(flows.items)))
        return len(flows.items)

    async def store_stock(_: object, *, market_code: str, flows: SimpleNamespace) -> int:
        stored.append((market_code, flows.trade_date, len(flows.items)))
        return len(flows.items)

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)
    monkeypatch.setattr(service, "store_institutional_market_flows", store_market)
    monkeypatch.setattr(service, "store_institutional_stock_flows", store_stock)

    run = _run("institutional_twse")
    run.edition_date = edition
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert status == "partial" and error == "twse_fetch_failures"
    # Stock flows: Monday 09-07 is a trading day, so the walk stores it and stops.
    assert fetched_stock == [edition]
    stock = cast(dict[str, Any], result["stock_flows"])
    assert stock["covered_trading_days"] == 1
    stock_statuses = [day["status"] for day in stock["days"]]
    assert stock_statuses == ["stored"]
    assert already_stored.isdisjoint(fetched_market)
    market = cast(dict[str, Any], result["market_flows"])
    assert market["covered_trading_days"] == 40
    statuses = [day["status"] for day in market["days"]]
    assert statuses.count("existing") == 4
    assert statuses.count("stored") == 36
    assert statuses.count("failed") == 1
    assert all(
        status == "no_data"
        for status, day in zip(statuses, market["days"], strict=True)
        if date.fromisoformat(day["trade_date"]).weekday() >= 5
    )
    # The walk stops at exactly 40 trading days; nothing older is asked for.
    assert min(fetched_market) == date.fromisoformat(market["days"][-1]["trade_date"])
    assert all(code == "tw_equity" for code, _, _ in stored)
    assert sum(count for _, day, count in stored if day == edition) == 15  # 10 stock + 5 market


@pytest.mark.asyncio
async def test_institutional_twse_is_refused_when_disabled() -> None:
    class Factory:
        pass

    # Explicit: the local apps/api/.env may switch the flag on for development.
    status, result, error = await execute_run(
        _run("institutional_twse"),
        cast(Any, _StubSessionFactory()),
        Settings(environment="test", twse_enabled=False),
    )
    assert (status, result, error) == ("failed", {}, "twse_unavailable")


@pytest.mark.asyncio
async def test_a_disabled_yahoo_still_refreshes_taiex_from_twse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """^TWII comes from the exchange, so the Yahoo flag must not gate it.

    The guard used to return before the TAIEX leg ran at all, which meant
    turning Yahoo off silently stopped a series that never touched Yahoo.
    """
    from daily_insights_api.modules.data_management import service

    async def refresh_taiex(*_: object, **__: object) -> TaiexRefresh:
        return TaiexRefresh(
            stored_count=21,
            as_of=date(2026, 9, 10),
            fetched_at=datetime(2026, 9, 11, tzinfo=UTC),
        )

    async def select_months(*_: object, **__: object) -> tuple[date, ...]:
        return (date(2026, 9, 1),)

    monkeypatch.setattr(service, "select_taiex_refresh_months", select_months)
    monkeypatch.setattr(service, "refresh_taiex_daily_bars", refresh_taiex)
    status, result, error = await execute_run(
        _run("index_yahoo"),
        cast(Any, _StubSessionFactory()),
        Settings(environment="test", yfinance_enabled=False, twse_enabled=True),
    )

    symbols = cast(list[dict[str, object]], result["symbols"])
    taiex = next(item for item in symbols if item["symbol"] == TAIEX_SYMBOL)
    assert taiex["status"] == "succeeded" and taiex["record_count"] == 21
    # The Yahoo symbols are reported as unavailable rather than omitted.
    assert all(
        item["error"] == "yfinance_unavailable"
        for item in symbols
        if item["symbol"] != TAIEX_SYMBOL
    )
    assert status == "partial" and error == "index_symbol_failures"


@pytest.mark.asyncio
async def test_a_disabled_twse_still_refreshes_the_yahoo_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.data_management import service

    async def refresh(*_: object, **__: object) -> tuple[list[object], list[object]]:
        return (
            [
                SimpleNamespace(
                    result=SimpleNamespace(
                        symbol="^DJI",
                        provenance=SimpleNamespace(
                            fetched_at=datetime(2026, 9, 11, tzinfo=UTC),
                            as_of=date(2026, 9, 10),
                        ),
                    ),
                    stored_count=7,
                )
            ],
            [],
        )

    monkeypatch.setattr(service, "YfinanceAdapter", lambda **_: object())
    monkeypatch.setattr(service, "refresh_index_daily_bars", refresh)
    status, result, _ = await execute_run(
        _run("index_yahoo"),
        cast(Any, _StubSessionFactory()),
        Settings(environment="test", yfinance_enabled=True, twse_enabled=False),
    )

    symbols = cast(list[dict[str, object]], result["symbols"])
    assert next(item for item in symbols if item["symbol"] == "^DJI")["status"] == "succeeded"
    taiex = next(item for item in symbols if item["symbol"] == TAIEX_SYMBOL)
    assert taiex["status"] == "failed" and taiex["error"] == "twse_unavailable"
    assert status == "partial"
