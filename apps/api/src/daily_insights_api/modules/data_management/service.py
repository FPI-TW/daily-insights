"""Queue ownership and execution for administrator data refreshes."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo

from anyio import Path
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings, is_placeholder_value
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
from daily_insights_api.modules.news.api import (
    EDITION_ORDER,
    create_news_client,
    edition_spec,
    effective_hostnames,
    publish_candidates,
    run_all_editions_with_outcomes,
    run_news_edition,
)
from daily_insights_api.modules.operations.api import sanitize_error_code
from daily_insights_api.modules.reports.api import (
    ACTIVE_LAUNCH_MANIFEST,
    LaunchMarketCode,
    MacroDashboard,
    MacroDashboardSnapshot,
    MorningMarketExecution,
    refresh_macro_dashboard,
    run_morning_report_edition,
)

TAIPEI = ZoneInfo("Asia/Taipei")
LEASE_FOR = timedelta(minutes=10)
HEARTBEAT_SECONDS = 30.0
# Both TWSE reports cover the listed market only; TPEx has its own endpoints.
# Rolling windows in trading days. The calendar ceilings are what ends a walk
# when TWSE answers "no data" for every date (blocked IP, outage); 40 trading
# days span ~56 calendar days.
MARKET_FLOW_LOOKBACK_TRADING_DAYS = 40
MARKET_FLOW_LOOKBACK_CALENDAR_DAYS = 80
# Per-stock flows are only ever read for the latest stored day, so one trading
# day is the whole need; a missed day is not backfilled. The calendar ceiling
# is what lets a run on a holiday reach the last trading day, which is already
# stored and therefore costs no request.
STOCK_FLOW_LOOKBACK_TRADING_DAYS = 1
STOCK_FLOW_LOOKBACK_CALENDAR_DAYS = 10
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


# A run lock prevents duplicate ownership of one row. This group lock spans the
# provider call itself, so cancelling a run cannot free the DB `running` slot
# and let another macro fetch start before the cancelled task has unwound.
MACRO_EXECUTION_LOCK_KEY = 5_239_842_371_114_209


ACTIVE_RUN_UNIQUE_CONSTRAINTS = frozenset(
    {
        "uq_data_management_runs_active_morning",
        "uq_data_management_runs_active_index",
        "uq_data_management_runs_active_institutional",
        "uq_data_management_runs_active_news",
        "uq_data_management_runs_active_news_publish",
        "uq_data_management_runs_active_manual_macro_dashboard",
        "uq_data_management_runs_running_macro_dashboard",
    }
)
AUTOMATIC_MACRO_EDITION_CONSTRAINT = "uq_data_management_runs_automatic_macro_dashboard_edition"
AutomaticMacroEnqueueResult = Literal["queued", "already_recorded"]
AUTOMATIC_NEWS_ALL_EDITION_CONSTRAINT = "uq_data_management_runs_automatic_news_all_edition"
AutomaticNewsEnqueueResult = Literal["queued", "already_recorded"]
NEWS_RETRY_OUTCOMES = frozenset({"partial", "unavailable", "failed"})
NEWS_RETRY_INTERVAL = timedelta(minutes=30)
NEWS_RETRY_UNTIL = time(hour=12)


def is_active_run_conflict(error: IntegrityError) -> bool:
    """Whether PostgreSQL rejected precisely one active-run unique index."""
    diagnostic = getattr(error.orig, "diag", None)
    return (
        getattr(error.orig, "sqlstate", None) == "23505"
        and getattr(diagnostic, "constraint_name", None) in ACTIVE_RUN_UNIQUE_CONSTRAINTS
    )


def _is_unique_conflict(error: IntegrityError, constraint_name: str) -> bool:
    diagnostic = getattr(error.orig, "diag", None)
    return (
        getattr(error.orig, "sqlstate", None) == "23505"
        and getattr(diagnostic, "constraint_name", None) == constraint_name
    )


def is_automatic_macro_edition_conflict(error: IntegrityError) -> bool:
    return _is_unique_conflict(error, AUTOMATIC_MACRO_EDITION_CONSTRAINT)


def is_automatic_news_all_edition_conflict(error: IntegrityError) -> bool:
    diagnostic = getattr(error.orig, "diag", None)
    return (
        getattr(error.orig, "sqlstate", None) == "23505"
        and getattr(diagnostic, "constraint_name", None) == AUTOMATIC_NEWS_ALL_EDITION_CONSTRAINT
    )


async def enqueue_run(
    database: AsyncSession,
    *,
    operation: str,
    market_code: str | None,
    requester_id: uuid.UUID | None,
    request_id: str | None,
    edition_date: date | None = None,
    payload: dict[str, Any] | None = None,
) -> DataManagementRun:
    if operation == "morning_market" and market_code not in {
        market.market_code for market in ACTIVE_LAUNCH_MANIFEST.markets
    }:
        raise ValueError("market_code is not in the active launch manifest")
    if operation == "news_market" and market_code not in EDITION_ORDER:
        raise ValueError("market_code is not a configured news edition")
    if operation not in {"morning_market", "news_market"} and market_code is not None:
        raise ValueError("market_code is only allowed for market operations")
    if (operation == "news_publish") != (payload is not None):
        raise ValueError("payload is required for, and only for, news_publish")
    run = DataManagementRun(
        operation=operation,
        market_code=market_code,
        edition_date=edition_date or taipei_today(),
        status="pending",
        requested_by_user_id=requester_id,
        payload=payload,
    )
    database.add(run)
    try:
        await database.flush()
    except IntegrityError as error:
        await database.rollback()
        if is_active_run_conflict(error):
            raise RunAlreadyActiveError from error
        raise
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


async def enqueue_automatic_macro_run(
    session_factory: async_sessionmaker[AsyncSession], *, edition_date: date
) -> AutomaticMacroEnqueueResult:
    """Queue at most one scheduler-created macro run for a Taipei edition."""
    async with session_factory() as database:
        existing = await database.scalar(
            select(DataManagementRun.id)
            .where(
                DataManagementRun.operation == "macro_dashboard",
                DataManagementRun.requested_by_user_id.is_(None),
                DataManagementRun.edition_date == edition_date,
            )
            .limit(1)
        )
        if existing is not None:
            return "already_recorded"
        try:
            await enqueue_run(
                database,
                operation="macro_dashboard",
                market_code=None,
                requester_id=None,
                request_id=None,
                edition_date=edition_date,
            )
        except (RunAlreadyActiveError, IntegrityError) as error:
            if isinstance(error, IntegrityError) and is_automatic_macro_edition_conflict(error):
                # The edition uniqueness index covers every terminal status, so
                # a restart must never create another automatic retry row.
                return "already_recorded"
            raise
    return "queued"


async def enqueue_automatic_news_all_run(
    session_factory: async_sessionmaker[AsyncSession], *, edition_date: date
) -> AutomaticNewsEnqueueResult:
    """Persist the one automatic 08:00 news obligation for an edition.

    This deliberately does not share the manual-news active-run slot: a manual
    request must not make the scheduler forget an edition just because a
    deployment happens while that request is executing.
    """
    async with session_factory.begin() as database:
        queued = await database.scalar(
            insert(DataManagementRun)
            .values(
                operation="news_all",
                market_code=None,
                edition_date=edition_date,
                status="pending",
                requested_by_user_id=None,
                scheduled_for=datetime.combine(edition_date, time(hour=8), TAIPEI),
            )
            .on_conflict_do_nothing()
            .returning(DataManagementRun.id)
        )
    return "queued" if queued is not None else "already_recorded"


async def cancel_run(
    database: AsyncSession,
    *,
    run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    request_id: str | None,
) -> DataManagementRun | None:
    """Atomically terminalize queued/in-flight work and revoke its lease."""
    run = await database.scalar(
        select(DataManagementRun).where(DataManagementRun.id == run_id).with_for_update()
    )
    if run is None or run.status not in {"pending", "running"}:
        await database.rollback()
        return None
    now = datetime.now(UTC)
    run.status = "cancelled"
    run.completed_at = now
    run.lease_owner = None
    run.lease_expires_at = None
    run.result = {
        "cancelled": True,
        "cancelled_at": now.isoformat(),
        "cancelled_by_user_id": str(actor_user_id),
    }
    run.error = "cancelled"
    record_audit_event(
        database,
        actor_user_id=actor_user_id,
        action="data_management.run_cancelled",
        target_type="data_management_run",
        target_id=str(run.id),
        after={"status": "cancelled", "operation": run.operation},
        request_id=request_id,
    )
    await database.commit()
    return run


async def claim_next_run(
    session_factory: async_sessionmaker[AsyncSession], owner: str, *, now: datetime | None = None
) -> DataManagementRun | None:
    now = now or datetime.now(UTC)
    try:
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
            local_now = now.astimezone(TAIPEI)
            expired_through = (
                local_now.date()
                if local_now.time().replace(tzinfo=None) > NEWS_RETRY_UNTIL
                else local_now.date() - timedelta(days=1)
            )
            expired_news_runs = (
                await database.scalars(
                    select(DataManagementRun)
                    .where(
                        DataManagementRun.status == "pending",
                        DataManagementRun.operation.in_(("news_all", "news_market")),
                        DataManagementRun.requested_by_user_id.is_(None),
                        DataManagementRun.scheduled_for.is_not(None),
                        DataManagementRun.edition_date <= expired_through,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for expired_run in expired_news_runs:
                expired_run.status = "cancelled"
                expired_run.completed_at = now
                expired_run.lease_owner = None
                expired_run.lease_expires_at = None
                expired_run.error = "news_window_expired"
                expired_run.result = {
                    "outcome": "expired",
                    "reason": "news_window_expired",
                    "expired_at": now.isoformat(),
                    "scheduled_for": (
                        expired_run.scheduled_for.isoformat()
                        if expired_run.scheduled_for is not None
                        else None
                    ),
                }
            manual_macro_active = select(DataManagementRun.id).where(
                DataManagementRun.operation == "macro_dashboard",
                DataManagementRun.requested_by_user_id.is_not(None),
                DataManagementRun.status.in_(("pending", "running")),
            )
            macro_running = select(DataManagementRun.id).where(
                DataManagementRun.operation == "macro_dashboard",
                DataManagementRun.status == "running",
            )
            due_automatic_news = (
                DataManagementRun.operation.in_(("news_all", "news_market"))
                & DataManagementRun.requested_by_user_id.is_(None)
                & (DataManagementRun.scheduled_for <= now)
            )
            run = await database.scalar(
                select(DataManagementRun)
                .where(
                    DataManagementRun.status == "pending",
                    or_(
                        DataManagementRun.scheduled_for.is_(None),
                        DataManagementRun.scheduled_for <= now,
                    ),
                    # Automatic macro editions queue behind an active manual
                    # run. Any macro claim also waits for the one running
                    # execution; the DB unique index handles races between
                    # workers without losing the pending row.
                    (
                        (DataManagementRun.operation != "macro_dashboard")
                        | (DataManagementRun.requested_by_user_id.is_not(None))
                        | ~manual_macro_active.exists()
                    ),
                    ((DataManagementRun.operation != "macro_dashboard") | ~macro_running.exists()),
                )
                .order_by(
                    # The automatic news chain has a hard noon deadline, so
                    # already-due work cannot sit behind sustained manual
                    # enqueueing. Existing macro/manual priority remains the
                    # tie-breaker for every other claimable run.
                    due_automatic_news.desc(),
                    # Prefer a manual macro run over its scheduled companion.
                    (DataManagementRun.operation == "macro_dashboard").desc(),
                    DataManagementRun.requested_by_user_id.is_(None),
                    DataManagementRun.created_at,
                )
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
    except IntegrityError as error:
        if is_active_run_conflict(error):
            return None
        raise


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


def _news_outcomes(run: DataManagementRun, result: dict[str, object]) -> dict[str, str]:
    """Normalize result data before deciding which automatic markets retry."""
    raw_outcomes = result.get("outcomes")
    if isinstance(raw_outcomes, dict) and all(
        isinstance(market, str) and isinstance(outcome, str)
        for market, outcome in raw_outcomes.items()
    ):
        return cast(dict[str, str], raw_outcomes)
    outcome = result.get("outcome")
    if not isinstance(outcome, str):
        outcome = "failed"
    markets = (
        (run.market_code,)
        if run.operation == "news_market" and run.market_code is not None
        else EDITION_ORDER
    )
    return {market: outcome for market in markets}


async def _enqueue_automatic_news_retries(
    database: AsyncSession,
    run: DataManagementRun,
    result: dict[str, object],
    *,
    now: datetime,
) -> None:
    """Add future-due retries while the completed row remains locked.

    The surrounding completion transaction verifies worker ownership first,
    terminalizes the current row, and inserts its successors as one atomic
    state transition.  PostgreSQL's historical uniqueness key provides an
    additional fence for lease recovery or concurrent scheduler processes.
    """
    if run.requested_by_user_id is not None or run.operation not in {"news_all", "news_market"}:
        return
    retry_at = now.astimezone(TAIPEI) + NEWS_RETRY_INTERVAL
    deadline = datetime.combine(run.edition_date, NEWS_RETRY_UNTIL, TAIPEI)
    if retry_at > deadline:
        return
    outcomes = _news_outcomes(run, result)
    for market_code, outcome in outcomes.items():
        if outcome not in NEWS_RETRY_OUTCOMES:
            continue
        await database.execute(
            insert(DataManagementRun)
            .values(
                operation="news_market",
                market_code=market_code,
                edition_date=run.edition_date,
                status="pending",
                requested_by_user_id=None,
                scheduled_for=retry_at,
            )
            .on_conflict_do_nothing()
        )


async def complete_news_run(
    session_factory: async_sessionmaker[AsyncSession],
    run: DataManagementRun,
    owner: str,
    *,
    status: str,
    result: dict[str, object],
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    """Complete a news run and atomically persist only its needed retries."""
    now = now or datetime.now(UTC)
    async with session_factory.begin() as database:
        current = await database.scalar(
            select(DataManagementRun).where(DataManagementRun.id == run.id).with_for_update()
        )
        if current is None or current.lease_owner != owner or current.status != "running":
            return
        await _enqueue_automatic_news_retries(database, current, result, now=now)
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


async def complete_macro_run(
    session_factory: async_sessionmaker[AsyncSession],
    run: DataManagementRun,
    owner: str,
    *,
    status: str,
    result: dict[str, object],
    error: str | None,
    dashboard: MacroDashboard,
) -> None:
    """Publish snapshot and terminal status together after ownership verification."""
    now = datetime.now(UTC)
    payload = dashboard.model_dump(mode="json")
    async with session_factory.begin() as database:
        current = await database.scalar(
            select(DataManagementRun).where(DataManagementRun.id == run.id).with_for_update()
        )
        if current is None or current.status != "running" or current.lease_owner != owner:
            return
        await database.execute(
            insert(MacroDashboardSnapshot)
            .values(
                scope_key="global_macro_bonds",
                fetched_at=dashboard.fetched_at,
                edition_date=run.edition_date,
                payload=payload,
            )
            .on_conflict_do_update(
                index_elements=[MacroDashboardSnapshot.scope_key],
                set_={
                    "fetched_at": dashboard.fetched_at,
                    "edition_date": run.edition_date,
                    "payload": payload,
                    "updated_at": now,
                },
            )
        )
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
    """Per-stock flows for the edition date only; market flows back to 40."""
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


async def _execute_news(
    run: DataManagementRun, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> tuple[str, dict[str, object], str | None]:
    api_key = settings.news_model_api_key
    if (
        not settings.daily_news_enabled
        or api_key is None
        or not api_key.get_secret_value().strip()
        or is_placeholder_value(api_key.get_secret_value())
    ):
        outcomes = {
            market: "unavailable"
            for market in (
                (cast(str, run.market_code),) if run.operation == "news_market" else EDITION_ORDER
            )
        }
        return "failed", {"outcome": "unavailable", "outcomes": outcomes}, "daily_news_unavailable"
    client = create_news_client(
        base_url=settings.model_api_base_url,
        api_key=api_key.get_secret_value(),
        model=settings.model_name,
        timeout_seconds=settings.model_timeout_seconds,
    )
    try:
        allowed = effective_hostnames(
            settings.news_extra_hostnames, settings.news_blocked_hostnames
        )
        if run.operation == "news_market":
            outcome = await run_news_edition(
                session_factory,
                client,
                run.edition_date,
                allowed_hostnames=allowed,
                fetch_timeout_seconds=settings.news_fetch_timeout_seconds,
                discovery_timeout_seconds=settings.news_discovery_timeout_seconds,
                spec=edition_spec(cast(str, run.market_code)),
            )
            outcomes = {cast(str, run.market_code): outcome}
        else:
            outcome, outcomes = await run_all_editions_with_outcomes(
                session_factory,
                client,
                run.edition_date,
                allowed_hostnames=allowed,
                fetch_timeout_seconds=settings.news_fetch_timeout_seconds,
                discovery_timeout_seconds=settings.news_discovery_timeout_seconds,
                # The scheduled run generates each market once: a reclaimed
                # row after a worker restart only fills in the markets it
                # never reached. Manual reruns regenerate on purpose.
                only_missing=run.requested_by_user_id is None,
            )
    except Exception as error:
        outcomes = {
            market: "failed"
            for market in (
                (cast(str, run.market_code),) if run.operation == "news_market" else EDITION_ORDER
            )
        }
        return "failed", {"outcome": "failed", "outcomes": outcomes}, sanitize_error(error)
    finally:
        await client.aclose()
    status = (
        "succeeded"
        if outcome in {"complete", "idempotent"}
        else "partial"
        if outcome == "partial"
        else "failed"
    )
    return (
        status,
        {"outcome": outcome, "outcomes": outcomes},
        None if status == "succeeded" else f"news_{outcome}",
    )


def _news_unavailable(settings: Settings) -> bool:
    api_key = settings.news_model_api_key
    return (
        not settings.daily_news_enabled
        or api_key is None
        or not api_key.get_secret_value().strip()
        or is_placeholder_value(api_key.get_secret_value())
    )


async def _execute_news_publish(
    run: DataManagementRun, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> tuple[str, dict[str, object], str | None]:
    """Publish admin-chosen candidates into their edition with the news client."""
    if _news_unavailable(settings):
        return "failed", {"outcome": "unavailable"}, "daily_news_unavailable"
    payload = run.payload or {}
    try:
        edition_id = uuid.UUID(str(payload["edition_id"]))
        candidate_ids = [uuid.UUID(str(value)) for value in payload["candidate_ids"]]
    except (KeyError, TypeError, ValueError):
        return "failed", {}, "news_publish_payload_invalid"
    assert settings.news_model_api_key is not None
    client = create_news_client(
        base_url=settings.model_api_base_url,
        api_key=settings.news_model_api_key.get_secret_value(),
        model=settings.model_name,
        timeout_seconds=settings.model_timeout_seconds,
    )
    try:
        return await publish_candidates(
            session_factory,
            client,
            run_id=run.id,
            edition_id=edition_id,
            candidate_ids=candidate_ids,
            actor_user_id=run.requested_by_user_id,
            allowed_hostnames=effective_hostnames(
                settings.news_extra_hostnames, settings.news_blocked_hostnames
            ),
            fetch_timeout_seconds=settings.news_fetch_timeout_seconds,
        )
    except Exception as error:
        return "failed", {}, sanitize_error(error)
    finally:
        await client.aclose()


async def _execute_macro(
    settings: Settings,
) -> tuple[str, dict[str, object], str | None, MacroDashboard]:
    dashboard = await refresh_macro_dashboard(settings)
    degraded = dashboard.calendar.status != "ok" or any(
        history.status != "ok" for history in dashboard.histories
    )
    status = "partial" if degraded else "succeeded"
    return (
        status,
        {
            "fetched_at": dashboard.fetched_at.isoformat(),
            "edition_date": dashboard.calendar.date.isoformat(),
        },
        "macro_sources_unavailable" if degraded else None,
        dashboard,
    )


async def execute_run(
    run: DataManagementRun, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> tuple[str, dict[str, object], str | None]:
    if run.operation == "index_yahoo":
        return await _execute_yahoo(run, session_factory, settings)
    if run.operation == "institutional_twse":
        return await _execute_institutional_twse(run, session_factory, settings)
    if run.operation == "news_publish":
        return await _execute_news_publish(run, session_factory, settings)
    if run.operation.startswith("news"):
        return await _execute_news(run, session_factory, settings)
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
            macro_lock_held = False
            try:
                if claimed_run.operation == "macro_dashboard":
                    await execution_database.execute(
                        select(func.pg_advisory_lock(MACRO_EXECUTION_LOCK_KEY))
                    )
                    macro_lock_held = True
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
                ownership_lost = asyncio.Event()

                async def heartbeater(
                    stopped: asyncio.Event = stop,
                    claimed_run_id: uuid.UUID = run_id,
                    lost: asyncio.Event = ownership_lost,
                ) -> None:
                    while not stopped.is_set():
                        try:
                            await asyncio.wait_for(stopped.wait(), timeout=heartbeat_seconds)
                        except TimeoutError:
                            if not await heartbeat_run(session_factory, claimed_run_id, owner):
                                lost.set()
                                return
                            if heartbeat_path is not None:
                                await heartbeat_path.touch()

                task = asyncio.create_task(heartbeater())
                dashboard: MacroDashboard | None = None
                execution_task = asyncio.create_task(
                    _execute_macro(settings)
                    if claimed_run.operation == "macro_dashboard"
                    else execute_run(claimed_run, session_factory, settings)
                )
                ownership_task = asyncio.create_task(ownership_lost.wait())
                try:
                    # Heartbeat failure includes durable cancellation. Stop the
                    # provider task promptly; incrementally committing work may
                    # retain prior commits but performs no further work.
                    await asyncio.wait(
                        {execution_task, ownership_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if ownership_lost.is_set() and not execution_task.done():
                        execution_task.cancel()
                        try:
                            await execution_task
                        except asyncio.CancelledError:
                            pass
                    if execution_task.cancelled():
                        continue
                    execution = await execution_task
                    if claimed_run.operation == "macro_dashboard":
                        outcome, result, error, dashboard = cast(
                            tuple[str, dict[str, object], str | None, MacroDashboard], execution
                        )
                    else:
                        outcome, result, error = cast(
                            tuple[str, dict[str, object], str | None], execution
                        )
                except Exception as caught:
                    outcome, result, error = "failed", {}, sanitize_error(caught)
                finally:
                    stop.set()
                    await task
                    ownership_task.cancel()
                if claimed_run.operation == "macro_dashboard" and dashboard is not None:
                    await complete_macro_run(
                        session_factory,
                        claimed_run,
                        owner,
                        status=outcome,
                        result=result,
                        error=error,
                        dashboard=dashboard,
                    )
                elif claimed_run.operation.startswith("news"):
                    await complete_news_run(
                        session_factory,
                        claimed_run,
                        owner,
                        status=outcome,
                        result=result,
                        error=error,
                    )
                else:
                    await complete_run(
                        session_factory,
                        claimed_run,
                        owner,
                        status=outcome,
                        result=result,
                        error=error,
                    )
            finally:
                if macro_lock_held:
                    await execution_database.execute(
                        select(func.pg_advisory_unlock(MACRO_EXECUTION_LOCK_KEY))
                    )
                await execution_database.execute(select(func.pg_advisory_unlock(key)))
                await execution_database.rollback()
        if once:
            return
