import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path as FileSystemPath
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anyio import Path

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.data_management.models import DataManagementRun
from daily_insights_api.modules.data_management.service import execute_run, worker_loop
from daily_insights_api.modules.data_sources.api import DataSourceError
from daily_insights_api.modules.markets.api import InstitutionalMarketFlow
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
async def test_yahoo_execution_uses_seven_day_period_and_serializes_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.data_management import service

    calls: dict[str, object] = {}

    class Factory:
        def begin(self) -> "Factory":
            return self

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    async def refresh(*_: object, **kwargs: object) -> tuple[list[object], list[object]]:
        calls.update(kwargs)
        return (
            [
                SimpleNamespace(
                    result=SimpleNamespace(
                        symbol="^TWII",
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
        cast(Any, Factory()),
        Settings(environment="test", yfinance_enabled=True),
    )
    assert status == "partial" and error == "yfinance_symbol_failures"
    assert calls["period"] == "7d"
    symbols = cast(list[dict[str, object]], result["symbols"])
    assert symbols[0]["record_count"] == 7 and symbols[0]["source_as_of"] == "2026-09-06"
    assert symbols[1]["error"] == "runtimeerror"


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

    class Factory:
        def begin(self) -> "Factory":
            return self

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

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
        run, cast(Any, Factory()), Settings(environment="test", twse_enabled=True)
    )

    assert status == "partial" and error == "twse_fetch_failures"
    # Stock flows: 7 trading days back from Monday 09-07 reach Friday 08-28,
    # crossing two weekends, so 11 calendar days are asked and 7 are stored.
    assert fetched_stock == [edition - timedelta(days=offset) for offset in range(11)]
    stock = cast(dict[str, Any], result["stock_flows"])
    assert stock["covered_trading_days"] == 7
    stock_statuses = [day["status"] for day in stock["days"]]
    assert stock_statuses.count("stored") == 7 and stock_statuses.count("no_data") == 4
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
        cast(Any, Factory()),
        Settings(environment="test", twse_enabled=False),
    )
    assert (status, result, error) == ("failed", {}, "twse_unavailable")
