"""Queue ownership and execution for administrator data refreshes."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol, cast
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
    DataSourceError,
    RetryPolicy,
    TwelveDataAdapter,
    TwelveDataTransport,
    TwseAdapter,
    YfinanceAdapter,
)
from daily_insights_api.modules.markets.api import (
    INSTITUTIONAL_MARKET_CODE,
    InstitutionalMarketFlow,
    InstitutionalStockFlow,
    refresh_index_daily_bars,
    store_institutional_market_flows,
    store_institutional_stock_flows,
    stored_flow_dates,
)
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
# Both TWSE reports cover the listed market only; TPEx has its own endpoints.
# Rolling windows in trading days. The calendar ceilings are what ends a walk
# when TWSE answers "no data" for every date (blocked IP, outage); 40 trading
# days span ~56 calendar days and 7 span ~11.
MARKET_FLOW_LOOKBACK_TRADING_DAYS = 40
MARKET_FLOW_LOOKBACK_CALENDAR_DAYS = 80
STOCK_FLOW_LOOKBACK_TRADING_DAYS = 7
STOCK_FLOW_LOOKBACK_CALENDAR_DAYS = 20
# TWSE being down looks the same on every date, so stop asking after three.
MAX_CONSECUTIVE_FAILURES = 3


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
    requester_id: uuid.UUID | None,
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


class _TwseFlows(Protocol):
    @property
    def items(self) -> tuple[object, ...]: ...
    @property
    def fetched_at(self) -> datetime: ...


@dataclass(slots=True)
class _FlowWalk:
    lookback_trading_days: int
    covered_trading_days: int = 0
    stored_rows: int = 0
    failures: int = 0
    aborted: bool = False
    days: list[dict[str, object]] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "lookback_trading_days": self.lookback_trading_days,
            "covered_trading_days": self.covered_trading_days,
            "aborted": self.aborted,
            "days": self.days,
        }


async def _fetch_flows_back[Flows: _TwseFlows](
    *,
    edition_date: date,
    existing: set[date],
    lookback_trading_days: int,
    lookback_calendar_days: int,
    fetch: Callable[[date], Awaitable[Flows]],
    store: Callable[[AsyncSession, Flows], Awaitable[int]],
    session_factory: async_sessionmaker[AsyncSession],
) -> _FlowWalk:
    """Walk back from `edition_date` one calendar day at a time until
    `lookback_trading_days` trading days are covered.

    Each date is its own fetch and its own transaction: a failure on one date is
    recorded and the walk continues, so a re-run only has to fill the holes.
    `MAX_CONSECUTIVE_FAILURES` in a row means the source itself is down and the
    walk stops rather than spending minutes asking the remaining dates.
    Dates already stored count toward the window without a fetch; non-trading
    dates are re-asked every run because a make-up trading day cannot be told
    from a holiday without asking.
    """
    walk = _FlowWalk(lookback_trading_days=lookback_trading_days)
    consecutive_failures = 0
    for offset in range(lookback_calendar_days):
        if walk.covered_trading_days >= lookback_trading_days:
            break
        day = edition_date - timedelta(days=offset)
        entry: dict[str, object] = {"trade_date": day.isoformat()}
        walk.days.append(entry)
        if day in existing:
            walk.covered_trading_days += 1
            entry["status"] = "existing"
            continue
        try:
            flows = await fetch(day)
        except DataSourceError as error:
            walk.failures += 1
            consecutive_failures += 1
            entry.update(status="failed", error=sanitize_error(error))
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # The source is down, not this one date. Asking the remaining
                # dates would cost minutes and tell us nothing; the next run
                # walks from today again and fills whatever is still missing.
                walk.aborted = True
                break
            continue
        consecutive_failures = 0
        if not flows.items:
            entry["status"] = "no_data"
            continue
        async with session_factory.begin() as database:
            count = await store(database, flows)
        walk.covered_trading_days += 1
        walk.stored_rows += count
        entry.update(status="stored", record_count=count, fetched_at=flows.fetched_at.isoformat())
    return walk


async def _execute_institutional_twse(
    run: DataManagementRun, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> tuple[str, dict[str, object], str | None]:
    """Per-stock flows back to 7 trading days; market flows back to 40."""
    if not settings.twse_enabled:
        return "failed", {}, "twse_unavailable"

    async with session_factory.begin() as database:
        existing_stock = await stored_flow_dates(
            database,
            flows=InstitutionalStockFlow,
            market_code=INSTITUTIONAL_MARKET_CODE,
            on_or_before=run.edition_date,
            limit=STOCK_FLOW_LOOKBACK_TRADING_DAYS,
        )
        existing_market = await stored_flow_dates(
            database,
            flows=InstitutionalMarketFlow,
            market_code=INSTITUTIONAL_MARKET_CODE,
            on_or_before=run.edition_date,
            limit=MARKET_FLOW_LOOKBACK_TRADING_DAYS,
        )

    async with TwseAdapter(
        base_url=settings.twse_base_url,
        timeout_seconds=settings.twse_timeout_seconds,
        request_interval_seconds=settings.twse_request_interval_seconds,
        max_attempts=settings.twse_retry_attempts,
    ) as adapter:
        stock = await _fetch_flows_back(
            edition_date=run.edition_date,
            existing=existing_stock,
            lookback_trading_days=STOCK_FLOW_LOOKBACK_TRADING_DAYS,
            lookback_calendar_days=STOCK_FLOW_LOOKBACK_CALENDAR_DAYS,
            fetch=adapter.get_stock_flows,
            store=lambda database, flows: store_institutional_stock_flows(
                database, market_code=INSTITUTIONAL_MARKET_CODE, flows=flows
            ),
            session_factory=session_factory,
        )
        market = await _fetch_flows_back(
            edition_date=run.edition_date,
            existing=existing_market,
            lookback_trading_days=MARKET_FLOW_LOOKBACK_TRADING_DAYS,
            lookback_calendar_days=MARKET_FLOW_LOOKBACK_CALENDAR_DAYS,
            fetch=adapter.get_market_flows,
            store=lambda database, flows: store_institutional_market_flows(
                database, market_code=INSTITUTIONAL_MARKET_CODE, flows=flows
            ),
            session_factory=session_factory,
        )

    # Status follows coverage, not just failures: TWSE answers a date it cannot
    # serve with HTTP 200 and a no-data stat, so a run that reached nothing can
    # look clean while covering zero trading days.
    failures = stock.failures + market.failures
    covered = stock.covered_trading_days + market.covered_trading_days
    wanted = stock.lookback_trading_days + market.lookback_trading_days
    if not covered:
        status, error_code = "failed", "twse_fetch_failures" if failures else "twse_no_coverage"
    elif failures:
        status, error_code = "partial", "twse_fetch_failures"
    elif covered < wanted:
        status, error_code = "partial", "twse_partial_coverage"
    else:
        status, error_code = "succeeded", None
    return (
        status,
        {
            "market_code": INSTITUTIONAL_MARKET_CODE,
            "request_interval_seconds": settings.twse_request_interval_seconds,
            "stock_flows": stock.summary(),
            "market_flows": market.summary(),
        },
        error_code,
    )


async def execute_run(
    run: DataManagementRun, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> tuple[str, dict[str, object], str | None]:
    if run.operation == "index_yahoo":
        return await _execute_yahoo(run, session_factory, settings)
    if run.operation == "institutional_twse":
        return await _execute_institutional_twse(run, session_factory, settings)
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
