"""Queue ownership and execution for administrator data refreshes."""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from anyio import Path
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.data_management.models import DataManagementRun
from daily_insights_api.modules.data_sources.api import (
    TRACKED_INDICES,
    RetryPolicy,
    TwelveDataAdapter,
    TwelveDataTransport,
    YfinanceAdapter,
)
from daily_insights_api.modules.markets.api import refresh_index_daily_bars
from daily_insights_api.modules.operations.api import sanitize_error_code
from daily_insights_api.modules.reports.api import (
    ACTIVE_LAUNCH_MANIFEST,
    LaunchMarketCode,
    MorningMarketExecution,
    run_morning_report_edition,
)

TAIPEI = ZoneInfo("Asia/Taipei")
LEASE_FOR = timedelta(minutes=10)
HEARTBEAT_SECONDS = 30.0


class RunAlreadyActiveError(Exception):
    pass


def taipei_today(now: datetime | None = None) -> date:
    return (now or datetime.now(TAIPEI)).astimezone(TAIPEI).date()


def sanitize_error(error: Exception) -> str:
    # Provider errors can contain URLs, upstream response fragments, and keys.
    # Store a stable code only; detailed diagnostics belong in protected logs.
    return sanitize_error_code(type(error).__name__)[:500]


def execution_lock_key(run_id: uuid.UUID) -> int:
    """Return a stable signed bigint key for one run's session advisory lock."""
    return int.from_bytes(run_id.bytes[:8], byteorder="big", signed=True)


async def enqueue_run(
    database: AsyncSession,
    *,
    operation: str,
    market_code: str | None,
    requester_id: uuid.UUID,
    request_id: str | None,
) -> DataManagementRun:
    if operation == "morning_market" and market_code not in {
        market.market_code for market in ACTIVE_LAUNCH_MANIFEST.markets
    }:
        raise ValueError("market_code is not in the active launch manifest")
    if operation != "morning_market" and market_code is not None:
        raise ValueError("market_code is only allowed for morning_market")
    run = DataManagementRun(
        operation=operation,
        market_code=market_code,
        edition_date=taipei_today(),
        status="pending",
        requested_by_user_id=requester_id,
    )
    database.add(run)
    try:
        await database.flush()
    except IntegrityError as error:
        await database.rollback()
        raise RunAlreadyActiveError from error
    record_audit_event(
        database,
        actor_user_id=requester_id,
        action="data_management.run_enqueued",
        target_type="data_management_run",
        target_id=str(run.id),
        after={
            "operation": operation,
            "market_code": market_code,
            "edition_date": run.edition_date.isoformat(),
        },
        request_id=request_id,
    )
    await database.commit()
    return run


async def claim_next_run(
    session_factory: async_sessionmaker[AsyncSession], owner: str
) -> DataManagementRun | None:
    now = datetime.now(UTC)
    async with session_factory.begin() as database:
        # An expired lease alone is not proof that the provider call stopped.
        # A live worker holds the session-scoped execution lock until it exits,
        # so only reclaim an expired row when an xact-scoped probe proves that
        # no execution session still owns the same lock.
        expired = (
            await database.scalars(
                select(DataManagementRun)
                .where(
                    DataManagementRun.status == "running",
                    DataManagementRun.lease_expires_at < now,
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        for expired_run in expired:
            available = await database.scalar(
                select(func.pg_try_advisory_xact_lock(execution_lock_key(expired_run.id)))
            )
            if available:
                expired_run.status = "pending"
                expired_run.lease_owner = None
                expired_run.lease_expires_at = None
        run = await database.scalar(
            select(DataManagementRun)
            .where(DataManagementRun.status == "pending")
            .order_by(DataManagementRun.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if run is None:
            return None
        run.status = "running"
        run.lease_owner = owner
        run.lease_expires_at = now + LEASE_FOR
        run.started_at = run.started_at or now
        return run


async def heartbeat_run(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID, owner: str
) -> bool:
    async with session_factory.begin() as database:
        updated = await database.scalar(
            update(DataManagementRun)
            .where(
                DataManagementRun.id == run_id,
                DataManagementRun.status == "running",
                DataManagementRun.lease_owner == owner,
            )
            .values(lease_expires_at=datetime.now(UTC) + LEASE_FOR)
            .returning(DataManagementRun.id)
        )
    return updated is not None


async def complete_run(
    session_factory: async_sessionmaker[AsyncSession],
    run: DataManagementRun,
    owner: str,
    *,
    status: str,
    result: dict[str, object],
    error: str | None = None,
) -> None:
    now = datetime.now(UTC)
    async with session_factory.begin() as database:
        current = await database.scalar(
            select(DataManagementRun).where(DataManagementRun.id == run.id).with_for_update()
        )
        if current is None or current.lease_owner != owner or current.status != "running":
            return
        current.status = status
        current.result = result
        current.error = error
        current.completed_at = now
        current.lease_owner = None
        current.lease_expires_at = None
        record_audit_event(
            database,
            actor_user_id=run.requested_by_user_id,
            action="data_management.run_completed",
            target_type="data_management_run",
            target_id=str(run.id),
            after={"status": status, "operation": run.operation},
        )


def _serialize_morning_execution(execution: MorningMarketExecution) -> dict[str, object]:
    source_date = execution.source_date
    return {
        "market_code": execution.market_code,
        "publication_action": execution.publication_action,
        "revision": execution.revision,
        "report_status": execution.report_status,
        "source_date": source_date.isoformat() if source_date is not None else None,
        "datasets": [
            {
                "dataset_key": item.dataset_key,
                "status": item.status,
                "fetched_at": item.fetched_at.isoformat() if item.fetched_at else None,
                "source_as_of": item.source_as_of.isoformat() if item.source_as_of else None,
                "record_count": item.record_count,
                "error": item.error,
            }
            for item in execution.datasets
        ],
    }


async def _execute_morning(
    run: DataManagementRun, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> tuple[str, dict[str, object], str | None]:
    if not settings.morning_reports_enabled or settings.twelve_data_api_key is None:
        return "failed", {"markets": []}, "morning_reports_unavailable"
    markets: tuple[LaunchMarketCode, ...] = (
        (cast(LaunchMarketCode, run.market_code),)
        if run.operation == "morning_market"
        else tuple(item.market_code for item in ACTIVE_LAUNCH_MANIFEST.markets)
    )
    outcomes: list[dict[str, object]] = []
    async with TwelveDataTransport(
        base_url=settings.twelve_data_base_url,
        api_key=settings.twelve_data_api_key,
        timeout_seconds=settings.twelve_data_timeout_seconds,
        retry_policy=RetryPolicy(max_attempts=settings.twelve_data_retry_attempts),
        max_concurrency=settings.twelve_data_max_concurrency,
    ) as transport:
        adapter = TwelveDataAdapter(transport)
        for market in markets:
            assert market is not None
            try:
                # This is intentionally the manual path: it does not consult
                # the scheduled publication guard, so equal input is a no-op
                # and changed input creates a new immutable revision.
                executions = await run_morning_report_edition(
                    session_factory, adapter, run.edition_date, market_codes=(market,)
                )
                outcomes.append(_serialize_morning_execution(executions[0]))
            except Exception as error:
                outcomes.append(
                    {
                        "market_code": market,
                        "publication_action": "failed",
                        "datasets": [],
                        "error": sanitize_error(error),
                    }
                )
    failures = [item for item in outcomes if item["publication_action"] == "failed"]
    degraded = [
        item
        for item in outcomes
        if item["publication_action"] != "failed"
        and item.get("report_status") in {"partial", "unavailable"}
    ]
    return (
        "failed"
        if len(failures) == len(outcomes)
        else "partial"
        if failures or degraded
        else "succeeded",
        {"markets": outcomes},
        sanitize_error(RuntimeError("morning market failure")) if failures else None,
    )


async def _execute_yahoo(
    run: DataManagementRun, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> tuple[str, dict[str, object], str | None]:
    if not settings.yfinance_enabled:
        return "failed", {"symbols": []}, "yfinance_unavailable"
    async with session_factory.begin() as database:
        refreshed, failures = await refresh_index_daily_bars(
            database,
            adapter=YfinanceAdapter(timeout_seconds=settings.yfinance_timeout_seconds),
            symbols=list(TRACKED_INDICES),
            period="7d",
        )
    symbols: list[dict[str, object]] = [
        {
            "symbol": entry.result.symbol,
            "status": "succeeded",
            "fetched_at": entry.result.provenance.fetched_at.isoformat(),
            "source_as_of": (
                entry.result.provenance.as_of.isoformat()
                if entry.result.provenance.as_of is not None
                else run.edition_date.isoformat()
            ),
            "record_count": entry.stored_count,
        }
        for entry in refreshed
    ]
    symbols.extend(
        {
            "symbol": item.symbol,
            "status": "failed",
            "error": sanitize_error(RuntimeError(item.error)),
        }
        for item in failures
    )
    return (
        "failed" if failures and not refreshed else "partial" if failures else "succeeded",
        {"period": "7d", "symbols": symbols},
        "yfinance_symbol_failures" if failures else None,
    )


async def execute_run(
    run: DataManagementRun, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> tuple[str, dict[str, object], str | None]:
    if run.operation == "index_yahoo":
        return await _execute_yahoo(run, session_factory, settings)
    return await _execute_morning(run, session_factory, settings)


async def worker_loop(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    once: bool = False,
    heartbeat_path: Path | None = None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> None:
    owner = f"data-management-worker-{uuid.uuid4()}"
    while True:
        if heartbeat_path is not None:
            await heartbeat_path.touch()
        run = await claim_next_run(session_factory, owner)
        if run is None:
            if once:
                return
            await asyncio.sleep(1)
            continue
        assert run is not None
        claimed_run = run
        run_id = claimed_run.id
        async with session_factory() as execution_database:
            key = execution_lock_key(run_id)
            await execution_database.execute(select(func.pg_advisory_lock(key)))
            try:
                still_owned = await execution_database.scalar(
                    select(DataManagementRun.id).where(
                        DataManagementRun.id == run_id,
                        DataManagementRun.status == "running",
                        DataManagementRun.lease_owner == owner,
                    )
                )
                if still_owned is None:
                    continue
                stop = asyncio.Event()

                async def heartbeater(
                    stopped: asyncio.Event = stop, claimed_run_id: uuid.UUID = run_id
                ) -> None:
                    while not stopped.is_set():
                        try:
                            await asyncio.wait_for(stopped.wait(), timeout=heartbeat_seconds)
                        except TimeoutError:
                            if not await heartbeat_run(session_factory, claimed_run_id, owner):
                                return
                            if heartbeat_path is not None:
                                await heartbeat_path.touch()

                task = asyncio.create_task(heartbeater())
                try:
                    outcome, result, error = await execute_run(
                        claimed_run, session_factory, settings
                    )
                except Exception as caught:
                    outcome, result, error = "failed", {}, sanitize_error(caught)
                finally:
                    stop.set()
                    await task
                await complete_run(
                    session_factory,
                    claimed_run,
                    owner,
                    status=outcome,
                    result=result,
                    error=error,
                )
            finally:
                await execution_database.execute(select(func.pg_advisory_unlock(key)))
                await execution_database.rollback()
        if once:
            return
