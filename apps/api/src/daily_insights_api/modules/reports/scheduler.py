import argparse
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from anyio import Path

from daily_insights_api.core.observability import emit_event

TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_RUN_AT = time(hour=8, minute=0)
FAILED_OUTCOME = "failed"
COMPLETED_OUTCOME = "complete"
EditionRunner = Callable[[date], Awaitable[str | None]]
Sleeper = Callable[[float], Awaitable[None]]
logger = logging.getLogger("daily_insights")


@dataclass(frozen=True)
class SameDayRetry:
    """Bounded same-day retry window for a daily edition.

    A runner that raises is always retried after ``interval`` until ``until``
    (Taipei time) on the edition date. Outcomes listed in ``retry_outcomes``
    (for example ``unavailable``) are retried the same way; every other
    outcome is final for that day.
    """

    interval: timedelta = timedelta(minutes=30)
    until: time = time(hour=12)
    retry_outcomes: frozenset[str] = frozenset()

    def retries(self, outcome: str) -> bool:
        return outcome == FAILED_OUTCOME or outcome in self.retry_outcomes

    def next_attempt(self, edition: date, now: datetime) -> datetime | None:
        local = now.astimezone(TAIPEI)
        attempt = local + self.interval
        deadline = datetime.combine(edition, self.until, TAIPEI)
        return attempt if attempt <= deadline else None


def due_edition(now: datetime, *, run_at: time = DEFAULT_RUN_AT) -> date | None:
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    local = now.astimezone(TAIPEI)
    return local.date() if local.time().replace(tzinfo=None) >= run_at else None


def next_run(now: datetime, *, run_at: time = DEFAULT_RUN_AT) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    local = now.astimezone(TAIPEI)
    candidate = datetime.combine(local.date(), run_at, TAIPEI)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


async def run_edition_guarded(runner: EditionRunner, edition: date) -> str:
    """Run one edition and convert an exception into the ``failed`` outcome.

    The scheduler process must outlive a failed edition; otherwise Docker
    restarts the container and the edition is retried immediately without
    any backoff.
    """
    try:
        outcome = await runner(edition)
    except Exception as error:
        logger.exception("scheduled edition %s failed", edition.isoformat())
        emit_event(
            "scheduler.edition.failed",
            edition_date=edition.isoformat(),
            error_code=type(error).__name__,
        )
        return FAILED_OUTCOME
    return outcome if outcome is not None else COMPLETED_OUTCOME


async def run_scheduler(
    runner: EditionRunner,
    *,
    now: Callable[[], datetime],
    sleep: Sleeper = asyncio.sleep,
    retry: SameDayRetry | None = None,
) -> None:
    last_requested: date | None = None
    retry_edition: date | None = None
    retry_due: datetime | None = None
    while True:
        current = now()
        edition = due_edition(current)
        if retry_due is not None and edition != retry_edition:
            retry_edition = retry_due = None
        if edition is not None and (edition != last_requested or retry_due is not None):
            outcome = await run_edition_guarded(runner, edition)
            last_requested = edition
            retry_edition = retry_due = None
            current = now()
            if retry is not None and retry.retries(outcome):
                retry_due = retry.next_attempt(edition, current)
                if retry_due is None:
                    emit_event(
                        "scheduler.retry.exhausted",
                        edition_date=edition.isoformat(),
                        outcome=outcome,
                    )
                else:
                    retry_edition = edition
                    emit_event(
                        "scheduler.retry.scheduled",
                        edition_date=edition.isoformat(),
                        outcome=outcome,
                        retry_at=retry_due.isoformat(),
                    )
        target = retry_due if retry_due is not None else next_run(current)
        await sleep(max(1.0, (target - current.astimezone(TAIPEI)).total_seconds()))


async def maintain_disabled_heartbeat(
    heartbeat: Path,
    *,
    sleep: Sleeper = asyncio.sleep,
) -> None:
    while True:
        await heartbeat.touch()
        await sleep(60)


async def run_with_heartbeat(
    runner: EditionRunner,
    edition_date: date,
    heartbeat: Path,
) -> str | None:
    try:
        return await runner(edition_date)
    finally:
        await heartbeat.touch()


def parse_args(
    args: list[str] | None = None,
    *,
    description: str = "Run the three-market morning-report scheduler",
    configure: Callable[[argparse.ArgumentParser], None] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--once", action="store_true", help="run one edition and exit")
    parser.add_argument("--edition-date", type=date.fromisoformat)
    if configure is not None:
        configure(parser)
    return parser.parse_args(args)
