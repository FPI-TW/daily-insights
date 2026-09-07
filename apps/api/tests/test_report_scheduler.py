import asyncio
from argparse import Namespace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path as FileSystemPath
from typing import cast

import pytest
from anyio import Path
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.core.models import Base
from daily_insights_api.modules.data_sources.api import TwelveDataAdapter
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    SameDayRetry,
    due_edition,
    next_run,
    parse_args,
    run_scheduler,
)
from daily_insights_api.scripts import run_morning_reports
from daily_insights_api.scripts.run_morning_reports import (
    maintain_disabled_heartbeat,
    run_manual_morning_report_edition,
    run_scheduled_morning_report_edition,
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


async def test_scheduled_restart_skips_all_published_markets_before_provider_work(
    tmp_path: FileSystemPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = Path(tmp_path / "published-heartbeat")
    events: list[tuple[str, dict[str, object]]] = []

    async def all_published(_: object, __: date) -> tuple[()]:
        return ()

    async def provider_must_not_run(*_: object, **__: object) -> tuple[()]:
        raise AssertionError("provider-backed report generation must not run")

    monkeypatch.setattr(run_morning_reports, "unpublished_morning_report_markets", all_published)
    monkeypatch.setattr(
        run_morning_reports, "run_scheduled_morning_report_markets", provider_must_not_run
    )
    monkeypatch.setattr(
        run_morning_reports,
        "emit_event",
        lambda name, **details: events.append((name, details)),
    )

    outcome = await run_scheduled_morning_report_edition(
        cast(async_sessionmaker[AsyncSession], object()),
        cast(TwelveDataAdapter, object()),
        date(2026, 8, 30),
        heartbeat,
    )

    assert outcome == "complete"
    assert await heartbeat.exists()
    assert events == [
        (
            "scheduler.edition.already_published",
            {
                "edition_date": "2026-08-30",
                "skipped_market_codes": ["global_macro_bonds", "crypto", "us_equity"],
            },
        )
    ]


async def test_scheduled_restart_processes_only_missing_markets(
    tmp_path: FileSystemPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = Path(tmp_path / "subset-heartbeat")
    calls: list[tuple[date, tuple[str, ...]]] = []
    events: list[tuple[str, dict[str, object]]] = []

    async def missing_subset(_: object, __: date) -> tuple[str, ...]:
        return ("crypto", "us_equity")

    async def record_run(
        _: object, __: object, edition: date, market_codes: tuple[str, ...]
    ) -> tuple[str, ...]:
        calls.append((edition, market_codes))
        return ("crypto",)

    monkeypatch.setattr(run_morning_reports, "unpublished_morning_report_markets", missing_subset)
    monkeypatch.setattr(run_morning_reports, "run_scheduled_morning_report_markets", record_run)
    monkeypatch.setattr(
        run_morning_reports,
        "emit_event",
        lambda name, **details: events.append((name, details)),
    )

    await run_scheduled_morning_report_edition(
        cast(async_sessionmaker[AsyncSession], object()),
        cast(TwelveDataAdapter, object()),
        date(2026, 8, 30),
        heartbeat,
    )

    assert calls == [(date(2026, 8, 30), ("crypto", "us_equity"))]
    assert events[0][1]["skipped_market_codes"] == ["global_macro_bonds", "crypto"]


async def test_manual_once_path_bypasses_durable_publication_guard(
    tmp_path: FileSystemPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = Path(tmp_path / "manual-heartbeat")
    calls: list[date] = []

    async def unexpected_guard(_: object, __: date) -> tuple[()]:
        raise AssertionError("manual --once must not inspect existing publications")

    async def record_run(_: object, __: object, edition: date) -> None:
        calls.append(edition)

    monkeypatch.setattr(run_morning_reports, "unpublished_morning_report_markets", unexpected_guard)
    monkeypatch.setattr(run_morning_reports, "run_morning_report_edition", record_run)

    outcome = await run_manual_morning_report_edition(
        cast(async_sessionmaker[AsyncSession], object()),
        cast(TwelveDataAdapter, object()),
        date(2026, 8, 30),
        heartbeat,
    )

    assert outcome == "complete"
    assert calls == [date(2026, 8, 30)]


class _Clock:
    """Deterministic clock whose sleep advances time and stops after a budget."""

    def __init__(self, start: datetime, *, stop_after_sleeps: int) -> None:
        self.current = start
        self.sleeps: list[float] = []
        self._stop_after = stop_after_sleeps

    def now(self) -> datetime:
        return self.current

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.current += timedelta(seconds=delay)
        if len(self.sleeps) >= self._stop_after:
            raise asyncio.CancelledError


_START = datetime(2026, 8, 30, 8, 0, tzinfo=TAIPEI)
_SECONDS_TO_NEXT_DAY_FROM_0830 = 23.5 * 3600


async def test_scheduler_survives_runner_exception_and_retries_within_window() -> None:
    clock = _Clock(_START, stop_after_sleeps=2)
    calls: list[date] = []

    async def runner(edition: date) -> str | None:
        calls.append(edition)
        if len(calls) == 1:
            raise RuntimeError("provider failed")
        return "complete"

    with pytest.raises(asyncio.CancelledError):
        await run_scheduler(runner, now=clock.now, sleep=clock.sleep, retry=SameDayRetry())

    assert calls == [date(2026, 8, 30), date(2026, 8, 30)]
    assert clock.sleeps == [30 * 60, _SECONDS_TO_NEXT_DAY_FROM_0830]


async def test_scheduler_without_retry_policy_logs_failure_and_waits_for_next_day() -> None:
    clock = _Clock(_START, stop_after_sleeps=1)

    async def runner(edition: date) -> str | None:
        raise RuntimeError("provider failed")

    with pytest.raises(asyncio.CancelledError):
        await run_scheduler(runner, now=clock.now, sleep=clock.sleep)

    assert clock.sleeps == [24 * 3600]


async def test_unavailable_outcome_is_retried_only_when_policy_opts_in() -> None:
    async def unavailable(edition: date) -> str | None:
        return "unavailable"

    default_clock = _Clock(_START, stop_after_sleeps=1)
    with pytest.raises(asyncio.CancelledError):
        await run_scheduler(
            unavailable, now=default_clock.now, sleep=default_clock.sleep, retry=SameDayRetry()
        )
    assert default_clock.sleeps == [24 * 3600]

    calls = 0

    async def counting_unavailable(edition: date) -> str | None:
        nonlocal calls
        calls += 1
        return "unavailable"

    opt_in = SameDayRetry(retry_outcomes=frozenset({"unavailable"}))
    retry_clock = _Clock(_START, stop_after_sleeps=9)
    with pytest.raises(asyncio.CancelledError):
        await run_scheduler(
            counting_unavailable, now=retry_clock.now, sleep=retry_clock.sleep, retry=opt_in
        )
    # 08:00 plus eight retries every 30 minutes reaches the 12:00 deadline,
    # after which the scheduler waits for the next day's edition.
    assert calls == 9
    assert retry_clock.sleeps[:8] == [30 * 60] * 8
    assert retry_clock.sleeps[8] == 20 * 3600


async def test_same_day_retry_stops_at_deadline() -> None:
    policy = SameDayRetry()
    assert policy.next_attempt(date(2026, 8, 30), datetime(2026, 8, 30, 11, 30, tzinfo=TAIPEI)) == (
        datetime(2026, 8, 30, 12, 0, tzinfo=TAIPEI)
    )
    assert policy.next_attempt(date(2026, 8, 30), datetime(2026, 8, 30, 11, 31, tzinfo=TAIPEI)) is (
        None
    )
    assert policy.retries("failed")
    assert not policy.retries("partial")
