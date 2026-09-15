import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest

from daily_insights_api.modules.data_management.api import RunAlreadyActiveError
from daily_insights_api.modules.reports.scheduler import TAIPEI, run_scheduler
from daily_insights_api.scripts import run_institutional_flows
from daily_insights_api.scripts.run_institutional_flows import RUN_AT, queue_run


class _SessionFactory:
    """Stands in for `async_sessionmaker`: `begin()` yields a throwaway session."""

    def begin(self) -> "_SessionFactory":
        return self

    def __call__(self) -> "_SessionFactory":
        return self

    async def __aenter__(self) -> object:
        return self

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

    run_id = uuid.uuid4()

    async def enqueue(_: object, **kwargs: object) -> object:
        calls.append(kwargs)
        return type(
            "Run",
            (),
            {"id": run_id, "status": "pending", "requested_by_user_id": None},
        )()

    async def wait_for_outcome(*_: object, **__: object) -> str:
        return "complete"

    monkeypatch.setattr(run_institutional_flows, "enqueue_run", enqueue)
    monkeypatch.setattr(run_institutional_flows, "_wait_for_outcome", wait_for_outcome)

    edition = date(2026, 9, 7)
    assert await queue_run(cast(Any, _SessionFactory()), edition_date=edition) == "complete"
    # Nobody asked for it, so the run carries no requester; the column is
    # nullable for exactly this case.
    assert calls == [
        {
            "operation": "institutional_twse",
            "market_code": None,
            "requester_id": None,
            "request_id": None,
            "edition_date": edition,
        }
    ]


@pytest.mark.asyncio
async def test_a_run_an_administrator_already_started_counts_as_todays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def enqueue(_: object, **__: object) -> object:
        raise RunAlreadyActiveError("institutional_twse")

    async def same_edition(*_: object) -> tuple[uuid.UUID, bool]:
        return uuid.uuid4(), False

    async def wait_for_outcome(*_: object, **__: object) -> str:
        return "complete"

    monkeypatch.setattr(run_institutional_flows, "enqueue_run", enqueue)
    monkeypatch.setattr(run_institutional_flows, "_same_edition_run", same_edition)
    monkeypatch.setattr(run_institutional_flows, "_wait_for_outcome", wait_for_outcome)

    # The existing run is observed; its terminal status, not the enqueue
    # conflict itself, decides whether the scheduler retries.
    assert await queue_run(cast(Any, _SessionFactory())) == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "outcome"),
    [
        ("succeeded", "complete"),
        ("partial", "failed"),
        ("failed", "failed"),
        ("cancelled", "complete"),
    ],
)
async def test_queue_run_returns_the_workers_terminal_outcome(
    monkeypatch: pytest.MonkeyPatch, terminal_status: str, outcome: str
) -> None:
    run_id = uuid.uuid4()

    class SessionFactory(_SessionFactory):
        async def scalar(self, _: object) -> str:
            return terminal_status

    async def enqueue(_: object, **__: object) -> object:
        return type(
            "Run",
            (),
            {"id": run_id, "status": "pending", "requested_by_user_id": None},
        )()

    monkeypatch.setattr(run_institutional_flows, "enqueue_run", enqueue)

    assert (
        await queue_run(
            cast(Any, SessionFactory()),
            sleep=lambda _: asyncio.sleep(0),
            poll_seconds=0.01,
            timeout_seconds=0.02,
        )
        == outcome
    )


@pytest.mark.asyncio
async def test_queue_run_times_out_a_stuck_worker_as_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid.uuid4()

    class SessionFactory(_SessionFactory):
        async def scalar(self, _: object) -> str:
            return "running"

    async def enqueue(_: object, **__: object) -> object:
        return type(
            "Run",
            (),
            {"id": run_id, "status": "pending", "requested_by_user_id": None},
        )()

    monkeypatch.setattr(run_institutional_flows, "enqueue_run", enqueue)

    assert (
        await queue_run(
            cast(Any, SessionFactory()),
            sleep=lambda _: asyncio.sleep(0),
            poll_seconds=1,
            timeout_seconds=1,
        )
        == "failed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded_status", ["succeeded", "cancelled"])
async def test_scheduler_restart_preserves_final_automatic_outcomes(
    monkeypatch: pytest.MonkeyPatch, recorded_status: str
) -> None:
    async def enqueue(*_: object, **__: object) -> object:
        return type(
            "Run",
            (),
            {
                "id": uuid.uuid4(),
                "status": recorded_status,
                "requested_by_user_id": None,
            },
        )()

    monkeypatch.setattr(run_institutional_flows, "enqueue_run", enqueue)

    assert (
        await queue_run(cast(Any, _SessionFactory()), edition_date=date(2026, 9, 7)) == "complete"
    )


@pytest.mark.asyncio
async def test_completed_manual_run_already_satisfies_the_same_edition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def enqueue(*_: object, **__: object) -> object:
        return type(
            "Run",
            (),
            {
                "id": uuid.uuid4(),
                "status": "succeeded",
                "requested_by_user_id": uuid.uuid4(),
            },
        )()

    monkeypatch.setattr(run_institutional_flows, "enqueue_run", enqueue)

    assert (
        await queue_run(cast(Any, _SessionFactory()), edition_date=date(2026, 9, 7)) == "complete"
    )


@pytest.mark.asyncio
async def test_cancelled_manual_active_run_does_not_cancel_the_automatic_obligation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid.uuid4()

    class SessionFactory(_SessionFactory):
        async def scalar(self, _: object) -> str:
            return "cancelled"

    async def enqueue(_: object, **__: object) -> object:
        raise RunAlreadyActiveError("institutional_twse")

    async def same_edition(*_: object) -> tuple[uuid.UUID, bool]:
        return run_id, False

    monkeypatch.setattr(run_institutional_flows, "enqueue_run", enqueue)
    monkeypatch.setattr(run_institutional_flows, "_same_edition_run", same_edition)

    assert await queue_run(cast(Any, SessionFactory()), edition_date=date(2026, 9, 7)) == "failed"


@pytest.mark.asyncio
async def test_manual_run_finishing_after_enqueue_conflict_satisfies_the_edition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid.uuid4()

    class SessionFactory(_SessionFactory):
        async def scalar(self, _: object) -> str:
            return "succeeded"

    async def enqueue(_: object, **__: object) -> object:
        raise RunAlreadyActiveError("institutional_twse")

    async def same_edition(*_: object) -> tuple[uuid.UUID, bool]:
        # The row is terminal now, but it is still the same row that occupied
        # the active slot when enqueue raised.
        return run_id, False

    monkeypatch.setattr(run_institutional_flows, "enqueue_run", enqueue)
    monkeypatch.setattr(run_institutional_flows, "_same_edition_run", same_edition)

    assert await queue_run(cast(Any, SessionFactory()), edition_date=date(2026, 9, 7)) == "complete"


@pytest.mark.asyncio
async def test_an_active_run_from_another_edition_is_retryable_not_todays_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def enqueue(_: object, **__: object) -> object:
        raise RunAlreadyActiveError("institutional_twse")

    async def no_current_edition_run(*_: object) -> None:
        return None

    async def wait_for_outcome(*_: object, **__: object) -> str:
        pytest.fail("a different edition must not be observed as today's run")

    monkeypatch.setattr(run_institutional_flows, "enqueue_run", enqueue)
    monkeypatch.setattr(run_institutional_flows, "_same_edition_run", no_current_edition_run)
    monkeypatch.setattr(run_institutional_flows, "_wait_for_outcome", wait_for_outcome)

    assert await queue_run(cast(Any, _SessionFactory()), edition_date=date(2026, 9, 7)) == "failed"


@pytest.mark.asyncio
async def test_the_scheduler_waits_for_the_afternoon_publication() -> None:
    # 08:00 is when the other schedulers fire; TWSE has published nothing yet.
    clock = _Clock(datetime(2026, 9, 7, 8, 0, tzinfo=TAIPEI), stop_after_sleeps=2)
    editions: list[date] = []

    async def runner(edition: date) -> str:
        editions.append(edition)
        return "complete"

    with pytest.raises(asyncio.CancelledError):
        await run_scheduler(runner, now=clock.now, sleep=clock.sleep, run_at=RUN_AT)

    assert RUN_AT.hour == 17
    # It sleeps until 17:00, runs once for that day, then sleeps a full day.
    assert clock.sleeps == [9 * 3600, 24 * 3600]
    assert editions == [date(2026, 9, 7)]


def test_the_scheduled_day_is_taipeis_rather_than_the_hosts() -> None:
    # No deployment sets TZ, so the container runs in UTC: 17:00 Taipei is
    # 09:00 UTC, and a UTC-based scheduler would fire eight hours early.
    assert datetime(2026, 9, 7, 9, 0, tzinfo=UTC).astimezone(TAIPEI).time() == RUN_AT
