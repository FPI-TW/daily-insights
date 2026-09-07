"""Queue the daily TWSE institutional-flow run at Taipei 17:00.

This one queues rather than fetches. The adapter spaces its own requests six
seconds apart, which only holds while a single walk is in flight, and that is
what the data-management queue guarantees: one institutional run at a time,
whether an administrator pressed the button or this scheduler did.

17:00 because TWSE publishes the day's figures around 16:00. A run queued
before that finds the date unpublished, records it and moves on, so the walk
would simply come back a day short.
"""

import asyncio
from datetime import date, datetime, time

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
RUN_AT = time(hour=17, minute=0)
# TWSE can publish late; keep trying into the evening rather than losing the day.
RETRY_POLICY = SameDayRetry(until=time(hour=21))


async def queue_run(session_factory: async_sessionmaker[AsyncSession]) -> str:
    """Queue one run, treating an already-active one as this day's run.

    The worker executes it, and the walk skips dates already stored, so a queue
    that lands on an afternoon TWSE has not published yet costs one request per
    missing date and the next day's run fills the gap.
    """
    try:
        async with session_factory.begin() as database:
            run = await enqueue_run(
                database,
                operation="institutional_twse",
                market_code=None,
                requester_id=None,
                request_id=None,
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

    async def queue(_: date) -> str:
        return await queue_run(session_factory)

    async def runner(run_date: date) -> str | None:
        return await run_with_heartbeat(queue, run_date, heartbeat)

    try:
        if args.once:
            await runner(datetime.now(TAIPEI).date())
        else:
            await run_scheduler(
                runner, now=lambda: datetime.now(TAIPEI), retry=RETRY_POLICY, run_at=RUN_AT
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
