from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any, Literal, cast

from sqlalchemy import and_, exists, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import aliased

from daily_insights_api.modules.orchestration.models import (
    FunctionAttempt,
    FunctionDependency,
    FunctionRun,
    JobRun,
    RoutineRun,
)
from daily_insights_api.modules.orchestration.registry import FUNCTION_BY_KEY
from daily_insights_api.modules.orchestration.service import (
    LEASE_DURATION,
    RECONCILIATION_BATCH_SIZE,
    SUCCESS_FUNCTION_STATUSES,
    TERMINAL_FUNCTION_STATUSES,
    function_dependencies_ready,
    job_dependencies_ready,
    next_retry_at,
    terminalize_expired_automatic_functions,
)

AttemptStatus = Literal["succeeded", "no_change", "partial", "unavailable", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class FunctionOutcome:
    status: AttemptStatus
    source_as_of: date | None = None
    fetched_at: datetime | None = None
    record_count: int | None = None
    payload_digest: str | None = None
    request_metadata: tuple[dict[str, Any], ...] = ()
    result: dict[str, Any] | None = None
    missing_scopes: tuple[str, ...] = ()
    error_code: str | None = None
    error_detail: str | None = None
    retryable: bool = False


@dataclass(slots=True)
class ClaimedFunction:
    connection: AsyncConnection
    function_run_id: uuid.UUID
    job_run_id: uuid.UUID
    attempt_id: uuid.UUID
    function_key: str
    provider_key: str
    edition_date: date
    fence_token: uuid.UUID
    deadline_at: datetime | None
    scope: dict[str, Any]


FunctionHandler = Callable[[ClaimedFunction], Awaitable[FunctionOutcome]]


class _SkipClaim(Exception):
    pass


def provider_lock_key(provider_key: str) -> int:
    digest = hashlib.sha256(f"daily-insights:provider:{provider_key}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def _try_provider_lock(connection: AsyncConnection, provider_key: str) -> bool:
    return bool(
        await connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": provider_lock_key(provider_key)},
        )
    )


async def _unlock_provider(connection: AsyncConnection, provider_key: str) -> None:
    await connection.execute(
        text("SELECT pg_advisory_unlock(:key)"),
        {"key": provider_lock_key(provider_key)},
    )


async def _ready_candidates(
    session_factory: async_sessionmaker[AsyncSession], now: datetime
) -> list[tuple[uuid.UUID, str]]:
    async with session_factory() as database:
        upstream = aliased(FunctionRun)
        blocked_by_dependency = exists(
            select(1)
            .select_from(FunctionDependency)
            .join(upstream, upstream.id == FunctionDependency.upstream_function_run_id)
            .where(
                FunctionDependency.downstream_function_run_id == FunctionRun.id,
                or_(
                    and_(
                        FunctionDependency.policy == "success",
                        upstream.status.not_in(SUCCESS_FUNCTION_STATUSES),
                    ),
                    and_(
                        FunctionDependency.policy == "terminal",
                        upstream.status.not_in(TERMINAL_FUNCTION_STATUSES),
                    ),
                ),
            )
        )
        per_provider = (
            select(
                FunctionRun.id.label("function_run_id"),
                FunctionRun.provider_key.label("provider_key"),
                FunctionRun.created_at.label("created_at"),
            )
            .join(JobRun, JobRun.id == FunctionRun.job_run_id)
            .where(
                JobRun.status.in_(("pending", "running")),
                ~blocked_by_dependency,
                or_(
                    and_(
                        FunctionRun.status.in_(("pending", "retry_wait")),
                        or_(
                            FunctionRun.next_attempt_at.is_(None),
                            FunctionRun.next_attempt_at <= now,
                        ),
                    ),
                    and_(
                        FunctionRun.status == "running",
                        FunctionRun.lease_expires_at < now,
                    ),
                ),
                or_(
                    JobRun.deadline_at.is_(None),
                    JobRun.deadline_at > now,
                    and_(
                        FunctionRun.function_key == "news_publish",
                        FunctionRun.status == "pending",
                        FunctionRun.attempt_count == 0,
                    ),
                ),
            )
            .distinct(FunctionRun.provider_key)
            .order_by(FunctionRun.provider_key, FunctionRun.created_at, FunctionRun.id)
            .subquery()
        )
        rows = (
            await database.execute(
                select(per_provider.c.function_run_id, per_provider.c.provider_key)
                .order_by(per_provider.c.created_at, per_provider.c.function_run_id)
                .limit(100)
            )
        ).all()
    return [(cast(uuid.UUID, row_id), cast(str, provider_key)) for row_id, provider_key in rows]


async def claim_ready_function(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    owner: str,
    now: datetime | None = None,
) -> ClaimedFunction | None:
    effective_now = now or datetime.now(UTC)
    async with session_factory() as deadline_database:
        await terminalize_expired_automatic_functions(deadline_database, now=effective_now)
    for function_run_id, provider_key in await _ready_candidates(session_factory, effective_now):
        connection = await engine.connect()
        try:
            if not await _try_provider_lock(connection, provider_key):
                await connection.close()
                continue
            # Session advisory locks survive transaction boundaries. End the
            # implicit transaction opened by SELECT before binding a session.
            await connection.commit()
            database = AsyncSession(bind=connection, expire_on_commit=False)
            try:
                async with database.begin():
                    job_run_id = await database.scalar(
                        select(FunctionRun.job_run_id).where(FunctionRun.id == function_run_id)
                    )
                    if job_run_id is None:
                        raise _SkipClaim
                    # Cancellation uses the same JobRun → FunctionRun →
                    # FunctionAttempt order, preventing an inverse-lock deadlock.
                    job_run = await database.scalar(
                        select(JobRun).where(JobRun.id == job_run_id).with_for_update()
                    )
                    function_run = await database.scalar(
                        select(FunctionRun)
                        .where(FunctionRun.id == function_run_id)
                        .with_for_update()
                    )
                    if function_run is None:
                        raise _SkipClaim
                    if job_run is None or not _claimable(function_run, job_run, effective_now):
                        raise _SkipClaim
                    if not await job_dependencies_ready(database, job_run.id):
                        raise _SkipClaim
                    if not await function_dependencies_ready(database, function_run.id):
                        raise _SkipClaim

                    if function_run.status == "running":
                        await _abandon_expired_attempt(database, function_run, effective_now)
                    fence_token = uuid.uuid4()
                    function_run.status = "running"
                    function_run.attempt_count += 1
                    function_run.lease_owner = owner
                    function_run.lease_token = fence_token
                    function_run.lease_expires_at = effective_now + LEASE_DURATION
                    function_run.heartbeat_at = effective_now
                    function_run.next_attempt_at = None
                    function_run.started_at = function_run.started_at or effective_now
                    if job_run.status == "pending":
                        job_run.status = "running"
                        job_run.started_at = effective_now
                    attempt = FunctionAttempt(
                        function_run_id=function_run.id,
                        attempt_number=function_run.attempt_count,
                        provider_key=function_run.provider_key,
                        function_key=function_run.function_key,
                        scope=_attempt_scope(function_run),
                        fence_token=fence_token,
                        status="running",
                        request_metadata=[],
                        started_at=effective_now,
                    )
                    database.add(attempt)
                    await database.flush()
                    claimed = ClaimedFunction(
                        connection=connection,
                        function_run_id=function_run.id,
                        job_run_id=job_run.id,
                        attempt_id=attempt.id,
                        function_key=function_run.function_key,
                        provider_key=function_run.provider_key,
                        edition_date=job_run.edition_date,
                        fence_token=fence_token,
                        deadline_at=job_run.deadline_at,
                        scope=_attempt_scope(function_run),
                    )
                await database.close()
                return claimed
            except _SkipClaim:
                await database.close()
                await _unlock_provider(connection, provider_key)
                await connection.close()
                continue
            except Exception:
                await database.close()
                raise
        except Exception:
            if not connection.closed:
                try:
                    await _unlock_provider(connection, provider_key)
                finally:
                    await connection.close()
            raise
    return None


def _attempt_scope(function_run: FunctionRun) -> dict[str, Any]:
    scope = dict(function_run.scope)
    if function_run.missing_scopes:
        scope["missing_scopes"] = list(function_run.missing_scopes)
    return scope


def _claimable(function_run: FunctionRun, job_run: JobRun, now: datetime) -> bool:
    if job_run.status not in {"pending", "running"}:
        return False
    if (
        job_run.deadline_at is not None
        and now >= job_run.deadline_at
        and not (
            function_run.function_key == "news_publish"
            and function_run.status == "pending"
            and function_run.attempt_count == 0
        )
    ):
        return False
    if function_run.status == "running":
        return function_run.lease_expires_at is not None and function_run.lease_expires_at < now
    return function_run.status in {"pending", "retry_wait"} and (
        function_run.next_attempt_at is None or function_run.next_attempt_at <= now
    )


async def _abandon_expired_attempt(
    database: AsyncSession, function_run: FunctionRun, now: datetime
) -> None:
    attempt = await database.scalar(
        select(FunctionAttempt)
        .where(
            FunctionAttempt.function_run_id == function_run.id,
            FunctionAttempt.status == "running",
        )
        .order_by(FunctionAttempt.attempt_number.desc())
        .limit(1)
        .with_for_update()
    )
    if attempt is not None:
        attempt.status = "failed"
        attempt.finished_at = now
        attempt.error_code = "lease_expired"
        attempt.error_detail = "the previous worker lease expired"


async def heartbeat_function(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
    *,
    now: datetime | None = None,
) -> bool:
    effective_now = now or datetime.now(UTC)
    async with session_factory() as database:
        result = await database.execute(
            update(FunctionRun)
            .where(
                FunctionRun.id == claimed.function_run_id,
                FunctionRun.status == "running",
                FunctionRun.lease_token == claimed.fence_token,
            )
            .values(
                heartbeat_at=effective_now,
                lease_expires_at=effective_now + LEASE_DURATION,
            )
        )
        await database.commit()
        return bool(cast(CursorResult[Any], result).rowcount)


async def finish_function(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
    outcome: FunctionOutcome,
    *,
    now: datetime | None = None,
) -> bool:
    effective_now = now or datetime.now(UTC)
    async with session_factory() as database:
        async with database.begin():
            job_run = await database.scalar(
                select(JobRun).where(JobRun.id == claimed.job_run_id).with_for_update()
            )
            if job_run is None:
                return False
            function_run = await database.scalar(
                select(FunctionRun)
                .where(
                    FunctionRun.id == claimed.function_run_id,
                    FunctionRun.status == "running",
                    FunctionRun.lease_token == claimed.fence_token,
                )
                .with_for_update()
            )
            attempt = await database.scalar(
                select(FunctionAttempt)
                .where(
                    FunctionAttempt.id == claimed.attempt_id,
                    FunctionAttempt.status == "running",
                    FunctionAttempt.fence_token == claimed.fence_token,
                )
                .with_for_update()
            )
            if function_run is None or attempt is None:
                return False
            attempt.status = outcome.status
            attempt.finished_at = effective_now
            attempt.source_as_of = outcome.source_as_of
            attempt.fetched_at = outcome.fetched_at
            attempt.record_count = outcome.record_count
            attempt.payload_digest = outcome.payload_digest
            attempt.request_metadata = list(outcome.request_metadata)
            attempt.result = outcome.result
            attempt.error_code = outcome.error_code
            attempt.error_detail = outcome.error_detail
            stored_result = _merge_partial_results(function_run.result, outcome.result)

            retry_at = (
                next_retry_at(
                    effective_now,
                    job_run.deadline_at,
                )
                if outcome.retryable and outcome.status in {"partial", "unavailable", "failed"}
                else None
            )
            if retry_at is not None:
                function_run.status = "retry_wait"
                function_run.next_attempt_at = retry_at
                function_run.missing_scopes = list(outcome.missing_scopes) or None
                if outcome.status == "partial":
                    function_run.result = stored_result
                function_run.error = outcome.error_code
            else:
                had_partial_result = bool(
                    await database.scalar(
                        select(FunctionAttempt.id)
                        .where(
                            FunctionAttempt.function_run_id == function_run.id,
                            FunctionAttempt.status == "partial",
                        )
                        .limit(1)
                    )
                )
                preserve_partial_result = had_partial_result and outcome.status in {
                    "unavailable",
                    "failed",
                }
                function_run.status = "partial" if preserve_partial_result else outcome.status
                function_run.completed_at = effective_now
                function_run.next_attempt_at = None
                function_run.missing_scopes = list(outcome.missing_scopes) or None
                if not preserve_partial_result:
                    function_run.result = stored_result
                function_run.error = outcome.error_code
            function_run.lease_owner = None
            function_run.lease_token = None
            function_run.lease_expires_at = None
            function_run.heartbeat_at = effective_now
        await aggregate_job(session_factory, claimed.job_run_id, now=effective_now)
        await aggregate_routines(session_factory, now=effective_now)
        return True


def _merge_partial_results(
    previous: dict[str, Any] | None, current: dict[str, Any] | None
) -> dict[str, Any] | None:
    if current is None:
        return previous
    if isinstance(previous, dict):
        previous_batch_id = previous.get("batch_id")
        current_batch_id = current.get("batch_id")
        previous_prepared = previous.get("prepared")
        current_prepared = current.get("prepared")
        if (
            isinstance(previous_batch_id, str)
            and isinstance(current_batch_id, str)
            and isinstance(previous_prepared, int)
            and not isinstance(previous_prepared, bool)
            and isinstance(current_prepared, int)
            and not isinstance(current_prepared, bool)
            and previous_prepared > current_prepared
        ):
            return dict(previous)
    merged = dict(current)
    previous_symbols = previous.get("symbols", []) if isinstance(previous, dict) else []
    current_symbols = current.get("symbols", [])
    if isinstance(previous_symbols, list) and isinstance(current_symbols, list):
        merged["symbols"] = list(
            dict.fromkeys(
                value for value in (*previous_symbols, *current_symbols) if isinstance(value, str)
            )
        )
    has_candidate_results = "candidates" in current or (
        isinstance(previous, dict) and "candidates" in previous
    )
    previous_candidates = previous.get("candidates", {}) if isinstance(previous, dict) else {}
    current_candidates = current.get("candidates", {})
    if (
        has_candidate_results
        and isinstance(previous_candidates, dict)
        and isinstance(current_candidates, dict)
    ):
        candidates = {**previous_candidates, **current_candidates}
        merged["candidates"] = candidates
        published = sum(
            value in {"published", "already_published"} for value in candidates.values()
        )
        merged["published"] = published
        merged["failed"] = len(candidates) - published
    return merged


async def aggregate_job(
    session_factory: async_sessionmaker[AsyncSession],
    job_run_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> None:
    effective_now = now or datetime.now(UTC)
    async with session_factory() as database:
        async with database.begin():
            job = await database.scalar(
                select(JobRun).where(JobRun.id == job_run_id).with_for_update()
            )
            if job is None or job.kind == "projection" or job.status == "cancelled":
                return
            functions = list(
                (
                    await database.scalars(
                        select(FunctionRun).where(FunctionRun.job_run_id == job.id)
                    )
                ).all()
            )
            statuses = [function.status for function in functions]
            if not statuses or any(
                status in {"pending", "running", "retry_wait"} for status in statuses
            ):
                return
            successful = sum(status in {"succeeded", "no_change"} for status in statuses)
            if successful == len(statuses):
                job.status = "succeeded"
            elif successful or any(status == "partial" for status in statuses):
                job.status = "partial"
            else:
                job.status = "failed"
            publish = next(
                (function for function in functions if function.function_key == "news_publish"),
                None,
            )
            if publish is not None:
                job.result = publish.result
            elif len(functions) == 1:
                job.result = functions[0].result
            job.completed_at = effective_now


async def aggregate_routines(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> None:
    effective_now = now or datetime.now(UTC)
    async with session_factory.begin() as database:
        has_jobs = exists(select(JobRun.id).where(JobRun.routine_run_id == RoutineRun.id))
        has_running_job = exists(
            select(JobRun.id).where(
                JobRun.routine_run_id == RoutineRun.id,
                JobRun.status == "running",
            )
        )
        has_active_job = exists(
            select(JobRun.id).where(
                JobRun.routine_run_id == RoutineRun.id,
                JobRun.status.in_(("pending", "running")),
            )
        )
        routines = list(
            (
                await database.scalars(
                    select(RoutineRun)
                    .where(
                        RoutineRun.status.in_(("pending", "running")),
                        has_jobs,
                        or_(
                            and_(RoutineRun.status == "pending", has_running_job),
                            ~has_active_job,
                        ),
                    )
                    .order_by(RoutineRun.created_at, RoutineRun.id)
                    .limit(RECONCILIATION_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        routine_ids = [routine.id for routine in routines]
        statuses_by_routine: dict[uuid.UUID, list[str]] = {
            routine_id: [] for routine_id in routine_ids
        }
        if routine_ids:
            status_rows = await database.execute(
                select(JobRun.routine_run_id, JobRun.status).where(
                    JobRun.routine_run_id.in_(routine_ids)
                )
            )
            for routine_id, status in status_rows:
                if routine_id is not None:
                    statuses_by_routine[routine_id].append(status)
        for routine in routines:
            statuses = statuses_by_routine[routine.id]
            if any(status == "running" for status in statuses):
                routine.status = "running"
                routine.started_at = routine.started_at or effective_now
            if not statuses or any(status in {"pending", "running"} for status in statuses):
                continue
            succeeded = sum(status == "succeeded" for status in statuses)
            if succeeded == len(statuses):
                routine.status = "succeeded"
            elif succeeded or any(status == "partial" for status in statuses):
                routine.status = "partial"
            else:
                routine.status = "failed"
            routine.completed_at = effective_now


async def reconcile_function_jobs(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> None:
    effective_now = now or datetime.now(UTC)
    async with session_factory() as database:
        has_functions = exists(select(FunctionRun.id).where(FunctionRun.job_run_id == JobRun.id))
        has_active_function = exists(
            select(FunctionRun.id).where(
                FunctionRun.job_run_id == JobRun.id,
                FunctionRun.status.not_in(TERMINAL_FUNCTION_STATUSES),
            )
        )
        ids = list(
            (
                await database.scalars(
                    select(JobRun.id)
                    .where(
                        JobRun.kind == "function",
                        JobRun.status.in_(("pending", "running")),
                        has_functions,
                        ~has_active_function,
                    )
                    .order_by(JobRun.created_at, JobRun.id)
                    .limit(RECONCILIATION_BATCH_SIZE)
                )
            ).all()
        )
    for job_run_id in ids:
        await aggregate_job(session_factory, job_run_id, now=effective_now)
    await aggregate_routines(session_factory, now=effective_now)


async def execute_claimed(
    claimed: ClaimedFunction,
    session_factory: async_sessionmaker[AsyncSession],
    handler: FunctionHandler,
) -> None:
    heartbeat_stop = asyncio.Event()

    async def heartbeats() -> None:
        while not heartbeat_stop.is_set():
            try:
                await asyncio.wait_for(heartbeat_stop.wait(), timeout=30)
            except TimeoutError:
                if not await heartbeat_function(session_factory, claimed):
                    return

    heartbeat_task = asyncio.create_task(heartbeats())
    cancelled = False
    try:
        try:
            outcome = await handler(claimed)
        except asyncio.CancelledError:
            outcome = FunctionOutcome(status="cancelled", error_code="cancelled")
            cancelled = True
        except Exception as error:
            provider_error_code = getattr(error, "error_code", None)
            outcome = FunctionOutcome(
                status="failed",
                error_code=(
                    provider_error_code
                    if isinstance(provider_error_code, str)
                    else type(error).__name__.lower()
                )[:100],
                error_detail=str(error)[:500],
                retryable=True,
            )
        finally:
            heartbeat_stop.set()
        definition = FUNCTION_BY_KEY.get(claimed.function_key)
        if definition is not None and not definition.retryable and outcome.retryable:
            outcome = replace(outcome, retryable=False)
        await finish_function(session_factory, claimed, outcome)
        if cancelled:
            raise asyncio.CancelledError
    finally:
        heartbeat_stop.set()
        try:
            await heartbeat_task
        finally:
            try:
                await _unlock_provider(claimed.connection, claimed.provider_key)
            finally:
                await claimed.connection.close()
