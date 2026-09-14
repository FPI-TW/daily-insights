"""Reconcile the daily news obligation throughout Taipei 08:00-12:00."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import date, datetime

from anyio import Path

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_daily_news_scheduler_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.logging import configure_logging
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.data_management.service import enqueue_automatic_news_all_run
from daily_insights_api.modules.news.api import automatic_window, cleanup_checkpoints
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    maintain_disabled_heartbeat,
    maintain_scheduler_heartbeat,
    parse_args,
)

HEARTBEAT_PATH = "/tmp/daily-news-heartbeat"


async def reconcile_news(
    runner: Callable[[date], Awaitable[str]],
    *,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    maintenance: Callable[[datetime], Awaitable[None]] | None = None,
) -> None:
    """DB uniqueness, not process memory, owns the initial daily obligation."""
    while True:
        current = now()
        if maintenance is not None:
            try:
                await maintenance(current)
            except Exception:
                emit_event(
                    "news.scheduler.maintenance_failed", error_code="checkpoint_cleanup_failed"
                )
        if automatic_window(current, current.astimezone(TAIPEI).date()):
            try:
                await runner(current.astimezone(TAIPEI).date())
            except Exception:
                # No model call is made here; keep reconciling the durable
                # obligation when DB connectivity recovers.
                emit_event("news.scheduler.reconcile_failed", error_code="news_enqueue_failed")
        await sleep(60)


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
        if not automatic_window(datetime.now(TAIPEI), edition_date):
            return "outside_window"
        await enqueue_automatic_news_all_run(sessions, edition_date=edition_date)
        await heartbeat.touch()
        return "complete"

    try:
        if args.once:
            await runner(args.edition_date or datetime.now(TAIPEI).date())
        else:
            await reconcile_news(
                runner,
                now=lambda: datetime.now(TAIPEI),
                maintenance=lambda now: cleanup_checkpoints(sessions, now),
            )
    finally:
        heartbeat_stop.set()
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
