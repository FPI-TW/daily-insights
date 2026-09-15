import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path as FileSystemPath
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from anyio import Path
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_news_integration import news_database as news_database

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
    TAIEX_SYMBOL,
    InstitutionalMarketFlow,
    TaiexRefresh,
)
from daily_insights_api.modules.reports.api import (
    LaunchMarketCode,
    MorningDatasetExecution,
    MorningMarketExecution,
)


async def _taiex_stored(*_: object, **__: object) -> dict[str, object]:
    """^TWII rides with the flows now; the tests below are about the walks."""
    return {"symbol": TAIEX_SYMBOL, "status": "succeeded", "record_count": 21}


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
async def test_automatic_twse_enqueue_returns_same_edition_manual_success_under_lock() -> None:
    existing = _run("institutional_twse")
    existing.status = "succeeded"
    existing.requested_by_user_id = uuid.uuid4()

    class Database:
        def __init__(self) -> None:
            self.locked = False

        async def execute(self, statement: object) -> None:
            assert "pg_advisory_xact_lock" in str(statement)
            self.locked = True

        async def scalar(self, _: object) -> DataManagementRun:
            assert self.locked
            return existing

        def add(self, _: object) -> None:
            pytest.fail("a satisfied automatic edition must not add another row")

    returned = await enqueue_run(
        cast(Any, Database()),
        operation="institutional_twse",
        market_code=None,
        requester_id=None,
        request_id=None,
        edition_date=existing.edition_date,
    )

    assert returned is existing


@pytest.mark.asyncio
async def test_cancel_run_terminalizes_pending_or_running_work_without_a_lease() -> None:
    run = _run("macro_dashboard")

    class Database:
        async def execute(self, _: object) -> None:
            return None

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

    monkeypatch.setattr(service, "YfinanceAdapter", lambda **_: object())
    monkeypatch.setattr(service, "refresh_index_daily_bars", refresh)
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
    # Nothing here speaks for ^TWII: the TWSE run reports it, next to the flows
    # it shares a client with.
    assert all(item["symbol"] != TAIEX_SYMBOL for item in symbols)
    # The operator reads this string in the back office, so it keeps the cause
    # instead of collapsing to a class name -- with secrets still redacted.
    assert symbols[1]["error"] == "api_key=[REDACTED]"


@pytest.mark.asyncio
async def test_the_twse_run_propagates_a_partial_taiex_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Months that did not land must reach the run's status.

    Ported from the index run, which no longer fetches ^TWII: the flows can be
    whole while the index is not, and an operator reading "succeeded" would
    never go looking for the missing months.
    """
    from daily_insights_api.modules.data_management import service

    edition = date(2026, 9, 7)

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

    async def select_months(*_: object, **__: object) -> tuple[date, ...]:
        return (date(2026, 8, 1), date(2026, 9, 1))

    async def refresh_taiex(*_: object, **__: object) -> TaiexRefresh:
        return TaiexRefresh(
            stored_count=21,
            as_of=date(2026, 9, 10),
            fetched_at=datetime(2026, 9, 11, tzinfo=UTC),
            failed_months=("2026-08: DataSourceContractError: boom",),
        )

    async def existing(*_: object, **__: object) -> set[date]:
        return _weekdays_before(edition, 40)

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)
    monkeypatch.setattr(service, "select_taiex_refresh_months", select_months)
    monkeypatch.setattr(service, "refresh_taiex_daily_bars", refresh_taiex)

    run = _run("institutional_twse")
    run.edition_date = edition
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert (status, error) == ("partial", "twse_index_failure")
    index = cast(dict[str, object], result["index"])
    assert index["status"] == "partial"
    assert "2026-08" in cast(str, index["error"])


@pytest.mark.asyncio
async def test_news_execution_routes_market_and_all_runs_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.data_management import service

    calls: list[str] = []
    observed: dict[str, object] = {}

    class Client:
        def __init__(self, **_: object) -> None:
            pass

        async def aclose(self) -> None:
            calls.append("closed")

    async def market(*_: object, **kwargs: object) -> str:
        calls.append(f"market:{cast(Any, kwargs['spec']).market_code}")
        return "complete"

    async def progress(*_: object) -> dict[str, object]:
        return {market: {"state": "completed", "progress": {"published": 1}} for market in observed}

    async def market_with_progress(*args: object, **kwargs: object) -> str:
        observed[str(cast(Any, kwargs["spec"]).market_code)] = True
        return await market(*args, **kwargs)

    monkeypatch.setattr(service, "create_news_client", lambda **_: Client())
    monkeypatch.setattr(service, "run_news_edition", market_with_progress)
    monkeypatch.setattr(service, "workflow_results", progress)
    settings = Settings(environment="test", daily_news_enabled=True, news_model_api_key="key")
    market_status, market_result, market_error = await execute_run(
        _run("news_market", "tw_equity"), cast(Any, None), settings
    )
    # The scheduled row (no requester) generates each market once; an
    # administrator's rerun regenerates on purpose.
    all_status, all_result, all_error = await execute_run(
        _run("news_all"), cast(Any, None), settings
    )
    manual = _run("news_all")
    manual.requested_by_user_id = uuid.uuid4()
    await execute_run(manual, cast(Any, None), settings)
    assert (market_status, market_error) == ("succeeded", None)
    assert (all_status, all_error) == ("succeeded", None)
    assert market_result["outcomes"] == {"tw_equity": "complete"}
    assert all_result["outcomes"] == {
        "global": "complete",
        "tw_equity": "complete",
        "us_equity": "complete",
    }
    assert calls == [
        "market:tw_equity",
        "closed",
        "market:global",
        "market:tw_equity",
        "market:us_equity",
        "closed",
        "market:global",
        "market:tw_equity",
        "market:us_equity",
        "closed",
    ]


@pytest.mark.asyncio
async def test_news_publish_execution_passes_the_payload_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.data_management import service

    calls: list[str] = []
    seen: dict[str, object] = {}

    class Client:
        def __init__(self, **_: object) -> None:
            pass

        async def aclose(self) -> None:
            calls.append("closed")

    async def publish(
        session_factory: object, client: object, **kwargs: object
    ) -> tuple[str, dict[str, object], str | None]:
        del session_factory, client
        calls.append("publish")
        seen.update(kwargs)
        return "partial", {"published": 1, "failed": 1}, "news_publish_partial"

    monkeypatch.setattr(service, "create_news_client", lambda **_: Client())
    monkeypatch.setattr(service, "publish_candidates", publish)
    monkeypatch.setattr(service, "workflow_results", AsyncMock(return_value={}))
    run = _run("news_publish")
    run.requested_by_user_id = uuid.uuid4()
    edition_id, candidate_id = uuid.uuid4(), uuid.uuid4()
    run.payload = {"edition_id": str(edition_id), "candidate_ids": [str(candidate_id)]}
    settings = Settings(
        environment="test",
        daily_news_enabled=True,
        news_model_api_key="key",
        news_fetch_timeout_seconds=7,
    )
    status, result, error = await execute_run(run, cast(Any, None), settings)
    assert (status, error) == ("partial", "news_publish_partial")
    assert result == {"published": 1, "failed": 1, "news": {}}
    assert calls == ["publish", "closed"]
    assert seen["run_id"] == run.id
    assert seen["edition_id"] == edition_id
    assert seen["candidate_ids"] == [candidate_id]
    assert seen["actor_user_id"] == run.requested_by_user_id
    assert seen["fetch_timeout_seconds"] == 7
    assert "www.theguardian.com" in cast(frozenset[str], seen["allowed_hostnames"])

    run.payload = {"edition_id": "not-a-uuid", "candidate_ids": []}
    assert await execute_run(run, cast(Any, None), settings) == (
        "failed",
        {},
        "news_publish_payload_invalid",
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_news_publish_execution_rejects_disabled_or_missing_model_key(
    news_database: async_sessionmaker[AsyncSession],
) -> None:
    for settings in (
        Settings(environment="test", daily_news_enabled=False, news_model_api_key="key"),
        Settings(environment="test", daily_news_enabled=True, news_model_api_key=None),
        Settings(environment="test", daily_news_enabled=True, news_model_api_key="CHANGE_ME_KEY"),
    ):
        status, result, error = await execute_run(_run("news_publish"), news_database, settings)
        assert (status, result, error) == (
            "failed",
            {"outcome": "unavailable"},
            "daily_news_unavailable",
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_news_execution_rejects_disabled_or_missing_model_key(
    news_database: async_sessionmaker[AsyncSession],
) -> None:
    run = _run("news_all")
    run.edition_date = datetime.now(ZoneInfo("Asia/Taipei")).date()
    run.requested_by_user_id = uuid.uuid4()
    status, result, error = await execute_run(
        run,
        news_database,
        Settings(environment="test", daily_news_enabled=False),
    )
    assert (status, error) == ("failed", "news_recovery_required")
    assert result["outcomes"] == {
        "global": "unavailable",
        "tw_equity": "unavailable",
        "us_equity": "unavailable",
    }


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
    """A re-run covers its whole window from storage and the refresh window, so
    `stored_rows` cannot tell a healthy window from an empty one. The edition
    date is stored and its refresh is the fetch that fails: the rows behind it
    still cover that day, so the run is partial over one failed refresh rather
    than short of its window."""
    from daily_insights_api.modules.data_management import service

    edition = date(2026, 9, 7)
    stored_market = _weekdays_before(edition, 39) | {edition}
    stored_stock = _weekdays_before(edition, 1)
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
            if trade_date == edition:
                raise DataSourceError("boom")
            # The refresh window re-asks stored weekdays; weekends never traded.
            return SimpleNamespace(
                trade_date=trade_date,
                items=() if trade_date.weekday() >= 5 else (object(),),
                fetched_at=datetime(2026, 9, 7, 9, tzinfo=UTC),
            )

        get_market_flows = get_stock_flows

    async def existing(*_: object, **kwargs: object) -> set[date]:
        # Copies, like the real query: the run drops dates from what it is given.
        return set(stored_market if kwargs["flows"] is InstitutionalMarketFlow else stored_stock)

    async def record(_: object, *, market_code: str, flows: SimpleNamespace) -> int:
        return len(flows.items)

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)
    monkeypatch.setattr(service, "store_institutional_stock_flows", record)
    monkeypatch.setattr(service, "store_institutional_market_flows", record)
    monkeypatch.setattr(service, "_refresh_taiex", _taiex_stored)

    run = _run("institutional_twse")
    run.edition_date = edition
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert (status, error) == ("partial", "twse_fetch_failures")
    assert cast(dict[str, Any], result["market_flows"])["covered_trading_days"] == 40
    assert cast(dict[str, Any], result["stock_flows"])["covered_trading_days"] == 1
    # The failed refresh did not cost the window a day, so the walk stopped on
    # the oldest date it already held instead of buying another one behind it.
    assert min(asked) >= min(stored_market)


@pytest.mark.asyncio
async def test_institutional_twse_refetches_the_newest_stored_days_and_trusts_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TWSE corrects a published report for days afterwards, so the newest
    market days are re-asked; the settled ones behind them stay untouched."""
    from daily_insights_api.modules.data_management import service

    edition = date(2026, 9, 7)
    stored_market = _weekdays_before(edition, 39) | {edition}
    refreshed = {edition}.union(
        sorted(stored_market, reverse=True)[: service.MARKET_FLOW_REFRESH_TRADING_DAYS]
    )
    settled = stored_market - refreshed
    asked: list[date] = []
    stored: list[date] = []

    class Adapter:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "Adapter":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_stock_flows(self, trade_date: date) -> SimpleNamespace:
            asked.append(trade_date)
            return SimpleNamespace(
                trade_date=trade_date,
                items=(object(),) if trade_date in stored_market else (),
                fetched_at=datetime(2026, 9, 7, 16, 30, tzinfo=UTC),
            )

        get_market_flows = get_stock_flows

    async def existing(*_: object, **kwargs: object) -> set[date]:
        # A copy, like the real query: the run drops dates from what it is given.
        return set(stored_market) if kwargs["flows"] is InstitutionalMarketFlow else {edition}

    async def record(_: object, *, market_code: str, flows: SimpleNamespace) -> int:
        stored.append(flows.trade_date)
        return 1

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)
    monkeypatch.setattr(service, "_refresh_taiex", _taiex_stored)
    monkeypatch.setattr(service, "store_institutional_stock_flows", record)
    monkeypatch.setattr(service, "store_institutional_market_flows", record)

    run = _run("institutional_twse")
    run.edition_date = edition
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert (status, error) == ("succeeded", None)
    # Seven market days re-asked and overwritten, the other 33 covered from
    # storage without a request, and the edition date re-asked by both walks.
    assert set(asked) & stored_market == refreshed
    assert not set(asked) & settled
    assert set(stored) == refreshed
    # The edition date is stored twice: once by each walk.
    assert len(stored) == len(refreshed) + 1
    assert asked.count(edition) == 2
    assert cast(dict[str, Any], result["market_flows"])["covered_trading_days"] == 40


@pytest.mark.asyncio
async def test_institutional_twse_keeps_stored_rows_when_the_refresh_comes_back_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refreshed date the source will not serve right now must not un-cover
    the window: the rows are still there, so the walk stops where it should
    instead of buying the same forty days further back."""
    from daily_insights_api.modules.data_management import service

    edition = date(2026, 9, 7)
    stored_market = _weekdays_before(edition, 39) | {edition}
    oldest_stored = min(stored_market)
    asked: list[date] = []
    stored: list[date] = []

    class Adapter:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "Adapter":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_stock_flows(self, trade_date: date) -> SimpleNamespace:
            asked.append(trade_date)
            # TWSE answers every date with its no-data stat, refreshed or not.
            return SimpleNamespace(
                trade_date=trade_date,
                items=(),
                fetched_at=datetime(2026, 9, 7, 16, 30, tzinfo=UTC),
            )

        get_market_flows = get_stock_flows

    async def existing(*_: object, **kwargs: object) -> set[date]:
        return set(stored_market) if kwargs["flows"] is InstitutionalMarketFlow else {edition}

    async def record(_: object, *, market_code: str, flows: SimpleNamespace) -> int:
        stored.append(flows.trade_date)
        return 1

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)
    monkeypatch.setattr(service, "_refresh_taiex", _taiex_stored)
    monkeypatch.setattr(service, "store_institutional_stock_flows", record)
    monkeypatch.setattr(service, "store_institutional_market_flows", record)

    run = _run("institutional_twse")
    run.edition_date = edition
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert (status, error) == ("succeeded", None)
    assert not stored
    # The per-stock window is one day wide, so a refresh that answers nothing
    # must rest on the stored day rather than walk back and buy it again.
    stock = cast(dict[str, Any], result["stock_flows"])
    assert stock["covered_trading_days"] == 1
    assert [(day["trade_date"], day["status"]) for day in stock["days"]] == [
        (edition.isoformat(), "kept")
    ]
    market = cast(dict[str, Any], result["market_flows"])
    assert market["covered_trading_days"] == 40
    # The stored days that answered nothing are marked, not silently counted as
    # a fetch, and the walk never reaches behind the window it already holds.
    statuses = [day["status"] for day in market["days"]]
    assert statuses.count("kept") == service.MARKET_FLOW_REFRESH_TRADING_DAYS
    assert statuses.count("existing") == 40 - service.MARKET_FLOW_REFRESH_TRADING_DAYS
    assert min(asked) >= oldest_stored


@pytest.mark.asyncio
async def test_institutional_twse_refreshes_the_stored_stock_day_on_a_non_trading_edition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduler runs every day, weekends included. On a Saturday the market
    walk corrects Friday, so the per-stock day it is read alongside has to be
    corrected too rather than left at whatever Friday first published."""
    from daily_insights_api.modules.data_management import service

    saturday = date(2026, 9, 12)
    friday = date(2026, 9, 11)
    asked: list[date] = []
    stored_stock: list[date] = []

    class Adapter:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "Adapter":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_stock_flows(self, trade_date: date) -> SimpleNamespace:
            asked.append(trade_date)
            return SimpleNamespace(
                trade_date=trade_date,
                items=(object(),) if trade_date.weekday() < 5 else (),
                fetched_at=datetime(2026, 9, 12, 17, tzinfo=UTC),
            )

        get_market_flows = get_stock_flows

    async def existing(*_: object, **kwargs: object) -> set[date]:
        return set() if kwargs["flows"] is InstitutionalMarketFlow else {friday}

    async def record_stock(_: object, *, market_code: str, flows: SimpleNamespace) -> int:
        stored_stock.append(flows.trade_date)
        return 1

    async def record_market(_: object, *, market_code: str, flows: SimpleNamespace) -> int:
        return 1

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)
    monkeypatch.setattr(service, "_refresh_taiex", _taiex_stored)
    monkeypatch.setattr(service, "store_institutional_stock_flows", record_stock)
    monkeypatch.setattr(service, "store_institutional_market_flows", record_market)

    run = _run("institutional_twse")
    run.edition_date = saturday
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert (status, error) == ("succeeded", None)
    assert stored_stock == [friday]
    stock = cast(dict[str, Any], result["stock_flows"])
    assert [(day["trade_date"], day["status"]) for day in stock["days"]] == [
        (saturday.isoformat(), "no_data"),
        (friday.isoformat(), "stored"),
    ]
    assert asked.count(friday) >= 1


@pytest.mark.asyncio
async def test_institutional_twse_with_taiex_and_failed_flows_is_partial(
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
    monkeypatch.setattr(service, "_refresh_taiex", _taiex_stored)

    run = _run("institutional_twse")
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert (status, error) == ("partial", "twse_fetch_failures")
    assert len(asked) == 2 * service.MAX_CONSECUTIVE_FAILURES
    for walk in ("stock_flows", "market_flows"):
        summary = cast(dict[str, Any], result[walk])
        assert summary["aborted"] is True
        assert len(cast(list[Any], summary["days"])) == service.MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_institutional_twse_outage_over_a_full_window_is_partial_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refresh window is asked first, so an outage spends the failure budget
    before the walk ever reaches a date it holds. A run that still has forty
    stored days has not lost them, and must not report as if it had."""
    from daily_insights_api.modules.data_management import service

    edition = date(2026, 9, 7)
    stored_market = _weekdays_before(edition, 39) | {edition}

    class Adapter:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "Adapter":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_stock_flows(self, _: date) -> SimpleNamespace:
            raise DataSourceError("503 Service Unavailable")

        get_market_flows = get_stock_flows

    async def existing(*_: object, **kwargs: object) -> set[date]:
        return set(stored_market) if kwargs["flows"] is InstitutionalMarketFlow else {edition}

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)
    monkeypatch.setattr(service, "_refresh_taiex", _taiex_stored)

    run = _run("institutional_twse")
    run.edition_date = edition
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert (status, error) == ("partial", "twse_fetch_failures")
    assert cast(dict[str, Any], result["market_flows"])["aborted"] is True


@pytest.mark.asyncio
async def test_institutional_twse_with_taiex_but_no_flow_day_is_partial(
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
    monkeypatch.setattr(service, "_refresh_taiex", _taiex_stored)

    run = _run("institutional_twse")
    status, result, error = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert (status, error) == ("partial", "twse_partial_coverage")
    assert cast(dict[str, Any], result["market_flows"])["covered_trading_days"] == 0


@pytest.mark.asyncio
async def test_institutional_twse_walks_back_to_forty_trading_days_refetching_only_the_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.data_management import service

    edition = date(2026, 9, 7)  # Monday
    # Fourteen weekdays are already stored. The newest of them fill the refresh
    # window and are re-asked because TWSE keeps correcting them; the four
    # behind those are settled and must count toward the window without a
    # request.
    already_stored = _weekdays_before(edition, 14)
    refreshed = set(
        sorted(already_stored, reverse=True)[: service.MARKET_FLOW_REFRESH_TRADING_DAYS]
    )
    settled = already_stored - refreshed
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
        # Only the market table has history; the stock table starts empty. The
        # copy matters: the run drops dates from the set it is given.
        return set(already_stored) if kwargs["flows"] is InstitutionalMarketFlow else set()

    async def store_market(_: object, *, market_code: str, flows: SimpleNamespace) -> int:
        stored.append((market_code, flows.trade_date, len(flows.items)))
        return len(flows.items)

    async def store_stock(_: object, *, market_code: str, flows: SimpleNamespace) -> int:
        stored.append((market_code, flows.trade_date, len(flows.items)))
        return len(flows.items)

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)
    monkeypatch.setattr(service, "_refresh_taiex", _taiex_stored)
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
    assert settled.isdisjoint(fetched_market)
    assert refreshed <= set(fetched_market)
    market = cast(dict[str, Any], result["market_flows"])
    assert market["covered_trading_days"] == 40
    statuses = [day["status"] for day in market["days"]]
    assert statuses.count("existing") == len(settled)
    assert statuses.count("stored") == 40 - len(settled)
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
async def test_a_disabled_yahoo_leaves_taiex_alone_because_it_is_not_this_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning Yahoo off must stop Yahoo and nothing else.

    ^TWII never touched Yahoo, and it no longer rides with the symbols that do:
    a run reaching for it here would hold a second TWSE client while the TWSE
    run holds the first, and the interval each of them keeps would be half the
    interval the exchange sees.
    """
    from daily_insights_api.modules.data_management import service

    async def unreachable(*_: object, **__: object) -> object:
        raise AssertionError("the Yahoo run must not reach TWSE")

    monkeypatch.setattr(service, "select_taiex_refresh_months", unreachable)
    monkeypatch.setattr(service, "refresh_taiex_daily_bars", unreachable)
    status, result, error = await execute_run(
        _run("index_yahoo"),
        cast(Any, _StubSessionFactory()),
        Settings(environment="test", yfinance_enabled=False, twse_enabled=True),
    )

    symbols = cast(list[dict[str, object]], result["symbols"])
    assert all(item["error"] == "yfinance_unavailable" for item in symbols)
    assert all(item["symbol"] != TAIEX_SYMBOL for item in symbols)
    assert status == "failed" and error == "index_symbol_failures"


@pytest.mark.asyncio
async def test_the_twse_run_refreshes_taiex_through_the_client_the_flows_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of putting ^TWII here: one client, one interval.

    The exchange is spaced inside a client, so the guarantee only holds while
    one client is talking to it. This asserts the index leg was handed the same
    adapter instance the flow walks were, rather than opening its own.
    """
    from daily_insights_api.modules.data_management import service

    edition = date(2026, 9, 7)
    adapters: list[object] = []
    taiex_adapters: list[object] = []

    class Adapter:
        def __init__(self, **_: object) -> None:
            adapters.append(self)

        async def __aenter__(self) -> "Adapter":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get_stock_flows(self, trade_date: date) -> SimpleNamespace:
            return SimpleNamespace(
                trade_date=trade_date, items=(), fetched_at=datetime(2026, 9, 7, 9, tzinfo=UTC)
            )

        get_market_flows = get_stock_flows

    async def refresh_taiex(
        _run_row: object, _factory: object, *, adapter: object
    ) -> dict[str, object]:
        taiex_adapters.append(adapter)
        return {"symbol": TAIEX_SYMBOL, "status": "succeeded", "record_count": 21}

    async def existing(*_: object, **__: object) -> set[date]:
        return _weekdays_before(edition, 40)

    monkeypatch.setattr(service, "TwseAdapter", Adapter)
    monkeypatch.setattr(service, "stored_flow_dates", existing)
    monkeypatch.setattr(service, "_refresh_taiex", refresh_taiex)

    run = _run("institutional_twse")
    run.edition_date = edition
    status, result, _ = await execute_run(
        run, cast(Any, _StubSessionFactory()), Settings(environment="test", twse_enabled=True)
    )

    assert status == "succeeded"
    # One client opened, and it is the one the index leg was given.
    assert len(adapters) == 1
    assert taiex_adapters == adapters
    index = cast(dict[str, object], result["index"])
    assert index["symbol"] == TAIEX_SYMBOL and index["record_count"] == 21
