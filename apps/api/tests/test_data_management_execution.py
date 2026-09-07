import asyncio
import uuid
from datetime import UTC, date, datetime
from pathlib import Path as FileSystemPath
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anyio import Path

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.data_management.models import DataManagementRun
from daily_insights_api.modules.data_management.service import execute_run, worker_loop
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
