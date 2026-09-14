"""Queue the daily TWSE institutional-flow run at Taipei 08:00, for yesterday.

This one queues rather than fetches. The adapter spaces its own requests six
seconds apart, which only holds while a single walk is in flight, and that is
what the data-management queue guarantees: one institutional run at a time,
whether an administrator pressed the button or this scheduler did.

08:00 like every other daily scheduler, asking for the previous day: TWSE
publishes a day's figures around 16:00, so by the next morning the day it is
asked for is one the source can actually serve.
"""

import asyncio
from datetime import date, datetime, timedelta

from anyio import Path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.data_management.api import RunAlreadyActiveError, enqueue_run
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    SameDayRetry,
    maintain_disabled_heartbeat,
    parse_args,
    run_scheduler,
    run_with_heartbeat,
)

__all__ = ["main", "queue_run"]

HEARTBEAT_PATH = "/tmp/institutional-flows-heartbeat"
# The edition the scheduler hands out is today's; the figures this run is for
# are the day before it.
EDITION_LAG = timedelta(days=1)
RETRY_POLICY = SameDayRetry()


async def queue_run(session_factory: async_sessionmaker[AsyncSession], *, run_date: date) -> str:
    """Queue one run for the trading day before `run_date`.

    `run_date` is the edition the scheduler hands out, which is the day it
    fires. TWSE publishes a day's figures around 16:00, so the day this morning
    run can actually be served is the one before it.

    An already-active run is treated as this day's. The worker executes it, and
    the walk asks only for the dates it is missing plus the newest few it
    re-asks because TWSE revises them, so a queue that lands on a date TWSE has
    not published yet costs one request and the next day's run fills the gap.
    """
    edition_date = run_date - EDITION_LAG
    try:
        async with session_factory.begin() as database:
            run = await enqueue_run(
                database,
                operation="institutional_twse",
                market_code=None,
                requester_id=None,
                request_id=None,
                edition_date=edition_date,
            )
        emit_event("institutional_flows.queued", run_id=str(run.id))
    except RunAlreadyActiveError:
        # Someone pressed the button minutes ago. That run covers today.
        emit_event("institutional_flows.already_active")
    return "complete"


async def main() -> None:
    args = parse_args(description="Queue the daily TWSE institutional-flow run")
    settings = get_settings()
    heartbeat = Path(HEARTBEAT_PATH)
    await heartbeat.touch()
    if not settings.twse_enabled and not args.once:
        await maintain_disabled_heartbeat(heartbeat)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    async def queue(edition: date) -> str:
        return await queue_run(session_factory, run_date=edition)

    async def runner(run_date: date) -> str | None:
        return await run_with_heartbeat(queue, run_date, heartbeat)

    try:
        if args.once:
            await runner(datetime.now(TAIPEI).date())
        else:
            await run_scheduler(runner, now=lambda: datetime.now(TAIPEI), retry=RETRY_POLICY)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
