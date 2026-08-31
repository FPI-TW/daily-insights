import asyncio
from argparse import Namespace
from datetime import UTC, date, datetime
from pathlib import Path as FileSystemPath

import pytest
from anyio import Path
from pydantic import SecretStr

from daily_insights_api.core.config import Settings
from daily_insights_api.core.models import Base
from daily_insights_api.modules.reports.scheduler import due_edition, next_run, parse_args
from daily_insights_api.scripts import run_morning_reports
from daily_insights_api.scripts.run_morning_reports import (
    maintain_disabled_heartbeat,
    run_with_heartbeat,
)


def test_due_edition_uses_taipei_day_boundary() -> None:
    assert due_edition(datetime(2026, 8, 29, 23, 59, tzinfo=UTC)) is None
    assert due_edition(datetime(2026, 8, 30, 0, 0, tzinfo=UTC)) == date(2026, 8, 30)


def test_next_run_handles_before_and_after_deadline() -> None:
    assert next_run(datetime(2026, 8, 29, 23, 0, tzinfo=UTC)).isoformat() == (
        "2026-08-30T08:00:00+08:00"
    )
    assert next_run(datetime(2026, 8, 30, 0, 1, tzinfo=UTC)).isoformat() == (
        "2026-08-31T08:00:00+08:00"
    )


def _settings() -> Settings:
    return Settings(
        environment="development",
        database_url="postgresql+psycopg://user:pass@localhost/database",
        session_secret=SecretStr("test-session-secret" * 3),
        password_pepper=SecretStr("test-password-pepper" * 3),
    )


def test_one_shot_cli_accepts_an_explicit_edition_date() -> None:
    args = parse_args(["--once", "--edition-date", "2026-08-30"])
    assert args.once is True
    assert args.edition_date == date(2026, 8, 30)


def test_scheduler_cli_registers_foreign_key_target_tables() -> None:
    assert "markets" in Base.metadata.tables
    assert "report_pipeline_runs" in Base.metadata.tables
    market_code = Base.metadata.tables["report_pipeline_runs"].c.market_code
    assert next(iter(market_code.foreign_keys)).column.table.name == "markets"


@pytest.mark.parametrize("key", ["CHANGE_ME_TWELVE_DATA_API_KEY", "   \t"])
async def test_scheduler_cli_rejects_unusable_key_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    settings = _settings().model_copy(update={"twelve_data_api_key": SecretStr(key)})
    monkeypatch.setattr(run_morning_reports, "get_settings", lambda: settings)
    monkeypatch.setattr(
        run_morning_reports,
        "parse_args",
        lambda: Namespace(
            once=True,
            edition_date=date(2026, 8, 30),
        ),
    )

    class UnexpectedTransport:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("transport must not be constructed for a placeholder key")

    monkeypatch.setattr(run_morning_reports, "TwelveDataTransport", UnexpectedTransport)

    with pytest.raises(SystemExit, match="API key is required"):
        await run_morning_reports.main()


async def test_disabled_scheduler_refreshes_heartbeat(tmp_path: FileSystemPath) -> None:
    heartbeat = Path(tmp_path / "disabled-heartbeat")

    async def stop_after_first_refresh(_: float) -> None:
        assert await heartbeat.exists()
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await maintain_disabled_heartbeat(heartbeat, sleep=stop_after_first_refresh)


async def test_enabled_runner_refreshes_heartbeat_even_after_failure(
    tmp_path: FileSystemPath,
) -> None:
    heartbeat = Path(tmp_path / "enabled-heartbeat")

    async def fail(_: date) -> None:
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        await run_with_heartbeat(fail, date(2026, 8, 30), heartbeat)
    assert await heartbeat.exists()
