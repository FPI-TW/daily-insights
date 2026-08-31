import argparse
import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_RUN_AT = time(hour=7, minute=0)
EditionRunner = Callable[[date], Awaitable[None]]


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


async def run_scheduler(
    runner: EditionRunner,
    *,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    last_requested: date | None = None
    while True:
        current = now()
        edition = due_edition(current)
        if edition is not None and edition != last_requested:
            await runner(edition)
            last_requested = edition
        delay = max(1.0, (next_run(current) - current.astimezone(TAIPEI)).total_seconds())
        await sleep(delay)


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the three-market morning-report scheduler")
    parser.add_argument("--once", action="store_true", help="run one edition and exit")
    parser.add_argument("--edition-date", type=date.fromisoformat)
    return parser.parse_args(args)
