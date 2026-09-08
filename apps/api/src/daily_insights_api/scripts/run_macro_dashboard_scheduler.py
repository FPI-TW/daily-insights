"""Enqueue one automatic macro-dashboard refresh each Taipei weekday at 08:00."""

import asyncio
from contextlib import suppress
from datetime import date, datetime

from anyio import Path

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.logging import configure_logging
from daily_insights_api.modules.data_management.service import enqueue_automatic_macro_run
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    maintain_scheduler_heartbeat,
    parse_args,
    run_scheduler,
)


async def main() -> None:
    configure_logging()
    args = parse_args(description="Run the macro dashboard queue scheduler")
    settings = get_settings()
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    heartbeat = Path("/tmp/macro-dashboard-scheduler-heartbeat")
    await heartbeat.touch()
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(maintain_scheduler_heartbeat(heartbeat, heartbeat_stop))

    async def runner(edition_date: date) -> str:
        await enqueue_automatic_macro_run(sessions, edition_date=edition_date)
        await heartbeat.touch()
        return "complete"

    try:
        if args.once:
            edition = args.edition_date or datetime.now(TAIPEI).date()
            if edition.weekday() >= 5:
                raise SystemExit("macro dashboard scheduler only runs on weekdays")
            await runner(edition)
        else:
            await run_scheduler(
                runner,
                now=lambda: datetime.now(TAIPEI),
                weekdays_only=True,
            )
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
