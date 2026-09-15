"""Queue the daily TWSE data run at Taipei 17:00.

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

17:00 because TWSE publishes the day's figures around 16:00. A run queued
before that finds the date unpublished, records it and moves on, so the walk
would simply come back a day short.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time
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
RUN_AT = time(hour=17, minute=0)
# TWSE can publish late; keep trying into the evening rather than losing the day.
RETRY_POLICY = SameDayRetry(until=time(hour=21))
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
    edition_date: date | None = None,
    heartbeat: Path | None = None,
    sleep: Sleep = asyncio.sleep,
    poll_seconds: float = RUN_POLL_SECONDS,
    timeout_seconds: float = RUN_WAIT_TIMEOUT_SECONDS,
) -> str:
    """Queue one run and wait for the worker's durable terminal outcome.

    The worker executes it -- ^TWII's months first, then the flow walks -- and
    the walk skips dates already stored, so a queue that lands on an afternoon
    TWSE has not published yet costs one request per missing date and the next
    attempt fills the gap. Partial and failed outcomes are returned as failed so
    ``SameDayRetry`` can actually retry provider degradation. An already-active
    run is observed instead of duplicated.
    """
    edition = edition_date or datetime.now(TAIPEI).date()
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

    async def queue(edition_date: date) -> str:
        return await queue_run(session_factory, edition_date=edition_date, heartbeat=heartbeat)

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
