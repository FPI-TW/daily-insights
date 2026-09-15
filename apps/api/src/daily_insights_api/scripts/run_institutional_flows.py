"""Queue the daily TWSE data run at Taipei 08:00, for the previous day.

Everything the exchange supplies goes through this one schedule: ^TWII's daily
bars as well as the institutional flows. Not because they belong together as
data -- ^TWII is displayed beside eight Yahoo indices -- but because they are
asked of the same host at a fixed interval, and that interval lives in the
client. Two schedulers holding two clients would each keep to six seconds and
together ask every three.

This one queues rather than fetches. The adapter spaces its own requests six
seconds apart, which only holds while a single walk is in flight, and that is
what the data-management queue guarantees: one TWSE run at a time, whether an
administrator pressed the button or this scheduler did.

08:00 like every other daily scheduler, asking for the previous day: TWSE
publishes a day's figures around 16:00, so by the next morning the day it is
asked for is one the source can actually serve.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import cast

from anyio import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.data_management.api import RunAlreadyActiveError, enqueue_run
from daily_insights_api.modules.data_management.models import DataManagementRun
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
RUN_POLL_SECONDS = 30.0
RUN_WAIT_TIMEOUT_SECONDS = 45 * 60.0
Sleep = Callable[[float], Awaitable[None]]


async def _same_edition_run(
    session_factory: async_sessionmaker[AsyncSession], edition_date: date
) -> tuple[uuid.UUID, bool] | None:
    """Find the run that caused an enqueue conflict, even if it just finished."""
    async with session_factory() as database:
        run = cast(
            DataManagementRun | None,
            await database.scalar(
                select(DataManagementRun)
                .where(
                    DataManagementRun.operation == "institutional_twse",
                    DataManagementRun.edition_date == edition_date,
                )
                .order_by(DataManagementRun.created_at.desc())
                .limit(1)
            ),
        )
    return None if run is None else (run.id, run.requested_by_user_id is None)


async def _wait_for_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    heartbeat: Path | None,
    sleep: Sleep,
    poll_seconds: float,
    timeout_seconds: float,
    retry_cancelled: bool = False,
) -> str:
    elapsed = 0.0
    while True:
        async with session_factory() as database:
            status = await database.scalar(
                select(DataManagementRun.status).where(DataManagementRun.id == run_id)
            )
        if heartbeat is not None:
            await heartbeat.touch()
        if status == "succeeded":
            emit_event("institutional_flows.completed", run_id=str(run_id), status=status)
            return "complete"
        if status in {"partial", "failed"}:
            emit_event("institutional_flows.completed", run_id=str(run_id), status=status)
            return "failed"
        if status == "cancelled":
            # Cancellation is an operator decision, not a provider failure.
            emit_event("institutional_flows.completed", run_id=str(run_id), status=status)
            return "failed" if retry_cancelled else "complete"
        if status not in {"pending", "running"} or elapsed >= timeout_seconds:
            emit_event(
                "institutional_flows.wait_failed",
                run_id=str(run_id),
                status=status or "missing",
            )
            return "failed"
        await sleep(poll_seconds)
        elapsed += poll_seconds


async def queue_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_date: date,
    heartbeat: Path | None = None,
    sleep: Sleep = asyncio.sleep,
    poll_seconds: float = RUN_POLL_SECONDS,
    timeout_seconds: float = RUN_WAIT_TIMEOUT_SECONDS,
) -> str:
    """Queue one run for the day before `run_date` and wait for the worker's
    durable terminal outcome.

    `run_date` is the edition the scheduler hands out, which is the day it
    fires. TWSE publishes a day's figures around 16:00, so the day this morning
    run can actually be served is the one before it.

    The worker executes it -- ^TWII's months first, then the flow walks -- and
    the walk asks only for the dates it is missing plus the newest few it
    re-asks because TWSE revises them, so a queue that lands on a date TWSE has
    not published yet costs one request and the next day's run fills the gap.
    Partial and failed outcomes are returned as failed so ``SameDayRetry`` can
    actually retry provider degradation. An already-active run is observed
    instead of duplicated.
    """
    edition = run_date - EDITION_LAG
    run_id: uuid.UUID | None
    retry_cancelled = False
    try:
        async with session_factory.begin() as database:
            run = await enqueue_run(
                database,
                operation="institutional_twse",
                market_code=None,
                requester_id=None,
                request_id=None,
                edition_date=edition,
            )
        run_id = run.id
        if run.status in {"succeeded", "cancelled"}:
            emit_event(
                "institutional_flows.already_recorded",
                edition_date=edition.isoformat(),
                run_id=str(run.id),
                status=run.status,
            )
            return "complete"
        retry_cancelled = run.requested_by_user_id is not None
        emit_event("institutional_flows.queued_or_observed", run_id=str(run.id))
    except RunAlreadyActiveError:
        # Someone pressed the button minutes ago. Observe that run rather than
        # declaring success before its provider outcome exists.
        same_edition = await _same_edition_run(session_factory, edition)
        run_id = same_edition[0] if same_edition is not None else None
        # Cancelling a manual run must not cancel today's automatic obligation.
        # Cancelling the automatic run itself remains an operator decision.
        retry_cancelled = same_edition is not None and not same_edition[1]
        emit_event(
            "institutional_flows.already_active",
            run_id=str(run_id) if run_id is not None else None,
        )
    if run_id is None:
        return "failed"
    return await _wait_for_outcome(
        session_factory,
        run_id,
        heartbeat=heartbeat,
        sleep=sleep,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
        retry_cancelled=retry_cancelled,
    )


async def main() -> None:
    args = parse_args(description="Queue the daily TWSE institutional-flow run")
    settings = get_settings()
    heartbeat = Path(HEARTBEAT_PATH)
    await heartbeat.touch()
    if not settings.twse_enabled and not args.once:
        await maintain_disabled_heartbeat(heartbeat)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    async def queue(run_date: date) -> str:
        return await queue_run(session_factory, run_date=run_date, heartbeat=heartbeat)

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
