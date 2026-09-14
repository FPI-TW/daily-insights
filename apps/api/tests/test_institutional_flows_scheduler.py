import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest

from daily_insights_api.modules.data_management.api import RunAlreadyActiveError
from daily_insights_api.modules.reports.scheduler import DEFAULT_RUN_AT, TAIPEI, run_scheduler
from daily_insights_api.scripts import run_institutional_flows
from daily_insights_api.scripts.run_institutional_flows import queue_run


class _SessionFactory:
    """Stands in for `async_sessionmaker`: `begin()` yields a throwaway session."""

    def begin(self) -> "_SessionFactory":
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_: object) -> None:
        return None


class _Clock:
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


@pytest.mark.asyncio
async def test_queue_run_asks_for_an_institutional_run_with_no_requester(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def enqueue(_: object, **kwargs: object) -> object:
        calls.append(kwargs)
        return type("Run", (), {"id": uuid.uuid4()})()

    monkeypatch.setattr(run_institutional_flows, "enqueue_run", enqueue)

    assert await queue_run(cast(Any, _SessionFactory()), run_date=date(2026, 9, 14)) == "complete"
    # Nobody asked for it, so the run carries no requester; the column is
    # nullable for exactly this case. The edition is the day before the one the
    # scheduler fired on: TWSE publishes around 16:00, so a morning run asking
    # for its own date would find nothing there.
    assert calls == [
        {
            "operation": "institutional_twse",
            "market_code": None,
            "requester_id": None,
            "request_id": None,
            "edition_date": date(2026, 9, 13),
        }
    ]


@pytest.mark.asyncio
async def test_a_run_an_administrator_already_started_counts_as_todays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def enqueue(_: object, **__: object) -> object:
        raise RunAlreadyActiveError("institutional_twse")

    monkeypatch.setattr(run_institutional_flows, "enqueue_run", enqueue)

    # Not an error: that run covers the same day, and treating it as a failure
    # would queue a second one the moment the retry window came round.
    assert await queue_run(cast(Any, _SessionFactory()), run_date=date(2026, 9, 14)) == "complete"


@pytest.mark.asyncio
async def test_the_scheduler_fires_in_the_morning_like_the_others() -> None:
    """No waiting for the afternoon: the day this asks for was published
    yesterday, so it runs on the same 08:00 schedule as everything else."""
    clock = _Clock(datetime(2026, 9, 7, 8, 0, tzinfo=TAIPEI), stop_after_sleeps=1)
    editions: list[date] = []

    async def runner(edition: date) -> str:
        editions.append(edition)
        return "complete"

    with pytest.raises(asyncio.CancelledError):
        await run_scheduler(runner, now=clock.now, sleep=clock.sleep)

    assert DEFAULT_RUN_AT.hour == 8
    # Due the moment it starts, then a full day's sleep.
    assert clock.sleeps == [24 * 3600]
    assert editions == [date(2026, 9, 7)]


def test_the_scheduled_day_is_taipeis_rather_than_the_hosts() -> None:
    # No deployment sets TZ, so the container runs in UTC: 08:00 Taipei is the
    # previous 00:00 UTC, and a UTC-based scheduler would fire eight hours late.
    assert datetime(2026, 9, 7, 0, 0, tzinfo=UTC).astimezone(TAIPEI).time() == DEFAULT_RUN_AT
