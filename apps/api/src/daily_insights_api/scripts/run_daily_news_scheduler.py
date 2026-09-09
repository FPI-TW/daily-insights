"""Queue daily news at the observed 08:00 Taipei boundary.

The worker owns provider calls and durable retry state.  This process only
records the automatic initial obligation, so a deployment restart after 08:00
cannot backfill or refetch the current edition.
"""

import asyncio
from contextlib import suppress
from datetime import date, datetime

from anyio import Path

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_daily_news_scheduler_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.logging import configure_logging
from daily_insights_api.modules.data_management.service import enqueue_automatic_news_all_run
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    SameDayRetry,
    maintain_disabled_heartbeat,
    maintain_scheduler_heartbeat,
    parse_args,
    run_scheduler,
)

HEARTBEAT_PATH = "/tmp/daily-news-heartbeat"


async def main() -> None:
    configure_logging()
    args = parse_args(description="Run the daily news queue scheduler")
    settings = get_daily_news_scheduler_settings()
    heartbeat = Path(HEARTBEAT_PATH)
    await heartbeat.touch()
    if not settings.daily_news_enabled:
        await maintain_disabled_heartbeat(heartbeat)

    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(maintain_scheduler_heartbeat(heartbeat, heartbeat_stop))

    async def runner(edition_date: date) -> str:
        await enqueue_automatic_news_all_run(sessions, edition_date=edition_date)
        await heartbeat.touch()
        return "complete"

    try:
        if args.once:
            await runner(args.edition_date or datetime.now(TAIPEI).date())
        else:
            await run_scheduler(
                runner,
                now=lambda: datetime.now(TAIPEI),
                retry=SameDayRetry(),
                catch_up_on_start=False,
            )
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
