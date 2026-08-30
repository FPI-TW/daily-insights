import asyncio
from datetime import UTC, date, datetime
from pathlib import Path as FileSystemPath

import pytest
from anyio import Path

from daily_insights_api.modules.reports.scheduler import due_edition, next_run
from daily_insights_api.scripts.run_morning_reports import (
    maintain_disabled_heartbeat,
    run_with_heartbeat,
)


def test_due_edition_uses_taipei_day_boundary() -> None:
    assert due_edition(datetime(2026, 8, 29, 22, 59, tzinfo=UTC)) is None
    assert due_edition(datetime(2026, 8, 29, 23, 0, tzinfo=UTC)) == date(2026, 8, 30)


def test_next_run_handles_before_and_after_deadline() -> None:
    assert next_run(datetime(2026, 8, 29, 22, 0, tzinfo=UTC)).isoformat() == (
        "2026-08-30T07:00:00+08:00"
    )
    assert next_run(datetime(2026, 8, 29, 23, 1, tzinfo=UTC)).isoformat() == (
        "2026-08-31T07:00:00+08:00"
    )


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
