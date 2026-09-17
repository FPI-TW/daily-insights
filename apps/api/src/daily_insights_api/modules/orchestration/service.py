from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.orchestration.models import (
    FunctionAttempt,
    FunctionDependency,
    FunctionRun,
    JobDependency,
    JobRun,
    RoutineRun,
)
from daily_insights_api.modules.orchestration.registry import (
    DAILY_ROUTINE,
    FUNCTION_BY_KEY,
    JOB_BY_KEY,
    MANUAL_MARKET_JOB_KEYS,
    REGISTRY_VERSION,
    JobDefinition,
    registry_digest,
    registry_snapshot,
)

TAIPEI = ZoneInfo("Asia/Taipei")
RETRY_INTERVAL = timedelta(minutes=30)
LEASE_DURATION = timedelta(minutes=10)
ROUTINE_ENQUEUE_LOCK = 5_239_842_371_114_300
RECONCILIATION_BATCH_SIZE = 100

SUCCESS_FUNCTION_STATUSES = frozenset(("succeeded", "no_change"))
TERMINAL_FUNCTION_STATUSES = frozenset(
    ("succeeded", "no_change", "partial", "unavailable", "failed", "cancelled")
)
TERMINAL_RUN_STATUSES = frozenset(("succeeded", "partial", "failed", "cancelled"))


def taipei_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(TAIPEI).date()


def routine_window(edition_date: date) -> tuple[datetime, datetime]:
    scheduled = datetime.combine(edition_date, time(DAILY_ROUTINE.scheduled_hour), tzinfo=TAIPEI)
    deadline = datetime.combine(edition_date, time(DAILY_ROUTINE.deadline_hour), tzinfo=TAIPEI)
    return scheduled, deadline


async def create_daily_routine(
    database: AsyncSession,
    *,
    edition_date: date | None = None,
) -> RoutineRun:
    effective_date = edition_date or taipei_today()
    await database.execute(select(func.pg_advisory_xact_lock(ROUTINE_ENQUEUE_LOCK)))
    existing = await database.scalar(
        select(RoutineRun).where(
            RoutineRun.routine_key == DAILY_ROUTINE.key,
            RoutineRun.edition_date == effective_date,
        )
    )
    if existing is not None:
        await database.commit()
        return existing

    snapshot = registry_snapshot()
    scheduled_for, deadline_at = routine_window(effective_date)
    routine = RoutineRun(
        routine_key=DAILY_ROUTINE.key,
        registry_version=REGISTRY_VERSION,
        registry_digest=registry_digest(),
        registry_snapshot=snapshot,
        edition_date=effective_date,
        scheduled_for=scheduled_for,
        deadline_at=deadline_at,
        status="pending",
    )
    database.add(routine)
    await database.flush()

    jobs: dict[str, JobRun] = {}
    for job_key in DAILY_ROUTINE.job_keys:
        definition = JOB_BY_KEY[job_key]
        job = _new_job_run(
            definition,
            edition_date=effective_date,
            trigger="automatic",
            requester_id=None,
            routine_run_id=routine.id,
            deadline_at=deadline_at,
            payload=None,
            snapshot=snapshot,
        )
        database.add(job)
        await database.flush()
        jobs[job_key] = job
        await _materialize_functions(database, job, definition)

    for dependency in DAILY_ROUTINE.dependencies:
        database.add(
            JobDependency(
                upstream_job_run_id=jobs[dependency.upstream_job_key].id,
                downstream_job_run_id=jobs[dependency.downstream_job_key].id,
                policy=dependency.policy,
            )
        )
    await database.commit()
    return routine


MANUAL_PROJECTIONS: dict[str, tuple[str, ...]] = {
    "global_macro_refresh": ("market_reports_publish", "macro_dashboard_publish"),
    "us_equity_refresh": ("market_reports_publish",),
}


async def enqueue_manual_job(
    database: AsyncSession,
    *,
    job_key: str,
    requester_id: uuid.UUID,
    payload: dict[str, Any] | None = None,
    edition_date: date | None = None,
) -> JobRun:
    definition = JOB_BY_KEY.get(job_key)
    if definition is None or "manual" not in definition.triggers:
        raise ValueError("job_key is not manually triggerable")
    effective_date = edition_date or taipei_today()
    snapshot = registry_snapshot()
    deadline_at = None
    job = _new_job_run(
        definition,
        edition_date=effective_date,
        trigger="manual",
        requester_id=requester_id,
        routine_run_id=None,
        deadline_at=deadline_at,
        payload=payload,
        snapshot=snapshot,
    )
    database.add(job)
    await database.flush()
    await _materialize_functions(database, job, definition)

    for projection_key in MANUAL_PROJECTIONS.get(job_key, ()):
        projection_definition = JOB_BY_KEY[projection_key]
        projection = _new_job_run(
            projection_definition,
            edition_date=effective_date,
            trigger="manual",
            requester_id=requester_id,
            routine_run_id=None,
            deadline_at=deadline_at,
            payload={"source_job_run_id": str(job.id), "requested_market_job": job_key},
            snapshot=snapshot,
        )
        database.add(projection)
        await database.flush()
        database.add(
            JobDependency(
                upstream_job_run_id=job.id,
                downstream_job_run_id=projection.id,
                policy="terminal",
            )
        )
    await database.flush()
    return job


def _new_job_run(
    definition: JobDefinition,
    *,
    edition_date: date,
    trigger: str,
    requester_id: uuid.UUID | None,
    routine_run_id: uuid.UUID | None,
    deadline_at: datetime | None,
    payload: dict[str, Any] | None,
    snapshot: dict[str, object],
) -> JobRun:
    return JobRun(
        routine_run_id=routine_run_id,
        job_key=definition.key,
        kind=definition.kind,
        trigger=trigger,
        automatic_key=definition.automatic_key if trigger == "automatic" else None,
        registry_version=REGISTRY_VERSION,
        registry_snapshot=snapshot,
        edition_date=edition_date,
        deadline_at=deadline_at,
        status="pending",
        requested_by_user_id=requester_id,
        payload=payload,
        next_attempt_at=None,
    )


async def _materialize_functions(
    database: AsyncSession, job: JobRun, definition: JobDefinition
) -> None:
    if definition.kind == "projection":
        return
    functions: dict[str, FunctionRun] = {}
    for step in definition.functions:
        function = FUNCTION_BY_KEY[step.function_key]
        run = FunctionRun(
            job_run_id=job.id,
            function_key=function.key,
            provider_key=function.provider_key,
            scope=job.payload or {},
            status="pending",
            attempt_count=0,
        )
        database.add(run)
        await database.flush()
        functions[step.function_key] = run
    for step in definition.functions:
        for upstream_key in step.depends_on:
            database.add(
                FunctionDependency(
                    upstream_function_run_id=functions[upstream_key].id,
                    downstream_function_run_id=functions[step.function_key].id,
                    policy=step.dependency_policy,
                )
            )


async def cancel_job_run(
    database: AsyncSession, *, job_run_id: uuid.UUID
) -> tuple[JobRun, str] | None:
    job = await database.scalar(select(JobRun).where(JobRun.id == job_run_id).with_for_update())
    if job is None or job.status in TERMINAL_RUN_STATUSES:
        return None
    previous_status = job.status
    job.status = "cancelled"
    job.completed_at = datetime.now(UTC)
    job.lease_token = None
    job.lease_owner = None
    job.lease_expires_at = None
    active_functions = list(
        await database.scalars(
            select(FunctionRun)
            .where(
                FunctionRun.job_run_id == job.id,
                FunctionRun.status.in_(("pending", "running", "retry_wait")),
            )
            .order_by(FunctionRun.id)
            .with_for_update()
        )
    )
    active_function_ids = [function.id for function in active_functions]
    await database.execute(
        update(FunctionAttempt)
        .where(FunctionAttempt.function_run_id.in_(active_function_ids))
        .where(FunctionAttempt.status == "running")
        .values(status="cancelled", finished_at=datetime.now(UTC), error_code="cancelled_by_admin")
    )
    await database.execute(
        update(FunctionRun)
        .where(FunctionRun.id.in_(active_function_ids))
        .values(
            status="cancelled",
            completed_at=datetime.now(UTC),
            next_attempt_at=None,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            error="cancelled_by_admin",
        )
    )
    await database.flush()
    return job, previous_status


async def job_dependencies_ready(database: AsyncSession, job_run_id: uuid.UUID) -> bool:
    dependencies = (
        await database.execute(
            select(JobDependency.policy, JobRun.status)
            .join(JobRun, JobRun.id == JobDependency.upstream_job_run_id)
            .where(JobDependency.downstream_job_run_id == job_run_id)
        )
    ).all()
    for policy, status in dependencies:
        if policy == "success" and status != "succeeded":
            return False
        if policy == "terminal" and status not in TERMINAL_RUN_STATUSES:
            return False
    return True


async def function_dependencies_ready(database: AsyncSession, function_run_id: uuid.UUID) -> bool:
    dependencies = (
        await database.execute(
            select(FunctionDependency.policy, FunctionRun.status)
            .join(FunctionRun, FunctionRun.id == FunctionDependency.upstream_function_run_id)
            .where(FunctionDependency.downstream_function_run_id == function_run_id)
        )
    ).all()
    for policy, status in dependencies:
        if policy == "success" and status not in SUCCESS_FUNCTION_STATUSES:
            return False
        if policy == "terminal" and status not in TERMINAL_FUNCTION_STATUSES:
            return False
    return True


async def terminalize_expired_automatic_functions(
    database: AsyncSession, *, now: datetime | None = None
) -> int:
    effective_now = now or datetime.now(UTC)
    expired = list(
        await database.scalars(
            select(FunctionRun)
            .where(
                or_(
                    FunctionRun.status.in_(("pending", "retry_wait")),
                    and_(
                        FunctionRun.status == "running",
                        FunctionRun.lease_expires_at <= effective_now,
                    ),
                ),
                FunctionRun.function_key != "news_publish",
                FunctionRun.job_run_id.in_(
                    select(JobRun.id).where(
                        JobRun.deadline_at.is_not(None),
                        JobRun.deadline_at <= effective_now,
                    )
                ),
            )
            .order_by(FunctionRun.created_at, FunctionRun.id)
            .limit(RECONCILIATION_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
    )
    expired_ids = [function_run.id for function_run in expired]
    running_ids = [function_run.id for function_run in expired if function_run.status == "running"]
    if running_ids:
        await database.execute(
            update(FunctionAttempt)
            .where(
                FunctionAttempt.function_run_id.in_(running_ids),
                FunctionAttempt.status == "running",
            )
            .values(
                status="failed",
                finished_at=effective_now,
                error_code="lease_expired",
                error_detail="worker lease expired at the job deadline",
            )
        )
    partial_ids = set(
        await database.scalars(
            select(FunctionAttempt.function_run_id)
            .where(
                FunctionAttempt.function_run_id.in_(expired_ids),
                FunctionAttempt.status == "partial",
            )
            .distinct()
        )
    )
    for function_run in expired:
        function_run.status = "partial" if function_run.id in partial_ids else "unavailable"
        function_run.completed_at = effective_now
        function_run.next_attempt_at = None
        function_run.lease_owner = None
        function_run.lease_token = None
        function_run.lease_expires_at = None
        function_run.heartbeat_at = effective_now
        function_run.error = "deadline_reached"
    await database.commit()
    return len(expired)


async def retry_due(function_run: FunctionRun, job_run: JobRun, now: datetime) -> bool:
    if function_run.status not in {"pending", "retry_wait"}:
        return False
    if function_run.next_attempt_at is not None and function_run.next_attempt_at > now:
        return False
    return job_run.deadline_at is None or now < job_run.deadline_at


def next_retry_at(now: datetime, deadline_at: datetime | None) -> datetime | None:
    candidate = now + RETRY_INTERVAL
    return candidate if deadline_at is None or candidate < deadline_at else None


async def active_manual_market_jobs(database: AsyncSession) -> list[JobRun]:
    return list(
        (
            await database.scalars(
                select(JobRun).where(
                    JobRun.trigger == "manual",
                    JobRun.job_key.in_(MANUAL_MARKET_JOB_KEYS),
                    or_(
                        JobRun.status.in_(("pending", "running")),
                        JobRun.lease_token.is_not(None),
                    ),
                )
            )
        ).all()
    )
