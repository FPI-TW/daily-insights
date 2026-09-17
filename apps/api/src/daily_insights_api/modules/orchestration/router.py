import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.data_management.api import DataManagementRun
from daily_insights_api.modules.identity.api import AuthContext, require_csrf_roles, require_roles
from daily_insights_api.modules.orchestration.models import (
    FunctionAttempt,
    FunctionDependency,
    FunctionRun,
    JobDependency,
    JobRun,
    RoutineRun,
)
from daily_insights_api.modules.orchestration.registry import (
    ADMIN_TRIGGER_JOB_KEYS,
    DAILY_ROUTINE,
    FUNCTIONS,
    JOBS,
    MANUAL_MARKET_JOB_KEYS,
    PROVIDERS,
    REGISTRY_VERSION,
    registry_digest,
)
from daily_insights_api.modules.orchestration.schemas import (
    FunctionAttemptResponse,
    FunctionCatalogItem,
    FunctionRunResponse,
    JobCatalogItem,
    JobRunCreate,
    JobRunList,
    JobRunResponse,
    LegacyRunList,
    LegacyRunResponse,
    OrchestrationCatalog,
    ProviderCatalogItem,
    RoutineRunList,
    RoutineRunResponse,
)
from daily_insights_api.modules.orchestration.service import (
    cancel_job_run,
    enqueue_manual_job,
    taipei_today,
)
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/admin/orchestration", tags=["orchestration"])
PAGE_SIZE = 10
AdminRead = Annotated[AuthContext, Depends(require_roles(SystemRole.ADMIN))]
AdminWrite = Annotated[AuthContext, Depends(require_csrf_roles(SystemRole.ADMIN))]


def _provider_ready(provider_key: str, settings: object) -> bool:
    if provider_key == "twelve_data":
        return bool(getattr(settings, "morning_reports_enabled", False))
    if provider_key == "yahoo_finance":
        return bool(getattr(settings, "yfinance_enabled", False))
    if provider_key == "twse":
        return bool(getattr(settings, "twse_enabled", False))
    if provider_key == "internal_services":
        return bool(
            getattr(settings, "daily_news_enabled", False)
            or getattr(settings, "analyst_viewpoints_enabled", False)
        )
    return True


@router.get("/catalog", response_model=OrchestrationCatalog)
async def catalog(
    request: Request,
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> OrchestrationCatalog:
    settings = request.app.state.settings
    recent_attempts = {
        attempt.function_key: attempt
        for attempt in (
            await database.scalars(
                select(FunctionAttempt)
                .distinct(FunctionAttempt.function_key)
                .order_by(
                    FunctionAttempt.function_key,
                    FunctionAttempt.started_at.desc(),
                    FunctionAttempt.id.desc(),
                )
            )
        ).all()
    }

    def provider_health(provider_key: str) -> FunctionAttempt | None:
        candidates = [
            recent_attempts[function.key]
            for function in FUNCTIONS
            if function.provider_key == provider_key and function.key in recent_attempts
        ]
        return max(candidates, key=lambda attempt: attempt.started_at, default=None)

    return OrchestrationCatalog(
        taipei_date=taipei_today(),
        registry_version=REGISTRY_VERSION,
        registry_digest=registry_digest(),
        providers=[
            ProviderCatalogItem(
                key=provider.key,
                display_name=provider.display_name,
                ready=_provider_ready(provider.key, settings),
                functions=[
                    function.key for function in FUNCTIONS if function.provider_key == provider.key
                ],
                last_status=(health.status if (health := provider_health(provider.key)) else None),
                last_attempt_at=health.started_at if health else None,
            )
            for provider in PROVIDERS
        ],
        functions=[
            FunctionCatalogItem(
                key=function.key,
                provider_key=function.provider_key,
                freshness_days=function.freshness_days,
                retryable=function.retryable,
                resources=list(function.resources),
                last_status=(
                    recent_attempts[function.key].status
                    if function.key in recent_attempts
                    else None
                ),
                last_attempt_at=(
                    recent_attempts[function.key].started_at
                    if function.key in recent_attempts
                    else None
                ),
            )
            for function in FUNCTIONS
        ],
        jobs=[
            JobCatalogItem(
                key=job.key,
                kind=job.kind,
                triggers=list(job.triggers),
                functions=[step.function_key for step in job.functions],
                projection_handler=job.projection_handler,
            )
            for job in JOBS
        ],
        routine_key=DAILY_ROUTINE.key,
        manual_market_jobs=list(MANUAL_MARKET_JOB_KEYS),
        features={
            "daily_news": bool(getattr(settings, "daily_news_enabled", False)),
            "analyst_viewpoints": bool(getattr(settings, "analyst_viewpoints_enabled", False)),
        },
    )


@router.post("/job-runs", response_model=JobRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job_run(
    payload: JobRunCreate,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> JobRunResponse:
    if payload.job_key not in ADMIN_TRIGGER_JOB_KEYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "job_key is not admin triggerable"
        )
    if payload.job_key.startswith("news_") and not request.app.state.settings.daily_news_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "daily news is unavailable")
    try:
        job = await enqueue_manual_job(
            database,
            job_key=payload.job_key,
            requester_id=actor.user.id,
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="orchestration.job_created",
        target_type="job_run",
        target_id=str(job.id),
        after={
            "job_key": job.job_key,
            "edition_date": job.edition_date.isoformat(),
            "status": job.status,
            "trigger": job.trigger,
        },
        request_id=request.state.request_id,
    )
    await database.commit()
    return await job_response(database, job)


@router.get("/job-runs", response_model=JobRunList)
async def list_job_runs(
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    page: int = Query(1, ge=1),
    job_key: str | None = None,
    job_group: Literal["news"] | None = None,
) -> JobRunList:
    filters = [JobRun.job_key == job_key] if job_key else []
    if job_group == "news":
        filters.append(
            or_(
                JobRun.job_key.startswith("news_"),
                JobRun.id.in_(
                    select(FunctionRun.job_run_id).where(
                        FunctionRun.function_key.startswith("news_")
                    )
                ),
            )
        )
    total = await database.scalar(select(func.count()).select_from(JobRun).where(*filters)) or 0
    jobs = (
        await database.scalars(
            select(JobRun)
            .where(*filters)
            .order_by(JobRun.created_at.desc(), JobRun.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).all()
    responses = await _job_responses(database, list(jobs))
    return JobRunList(
        items=responses,
        page=page,
        page_size=PAGE_SIZE,
        total=total,
        has_more=page * PAGE_SIZE < total,
    )


@router.get("/job-runs/{job_run_id}", response_model=JobRunResponse)
async def get_job_run(
    job_run_id: uuid.UUID,
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> JobRunResponse:
    job = await database.get(JobRun, job_run_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job run not found")
    return await job_response(database, job)


@router.post("/job-runs/{job_run_id}/cancel", response_model=JobRunResponse)
async def cancel_existing_job_run(
    job_run_id: uuid.UUID,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> JobRunResponse:
    cancelled = await cancel_job_run(database, job_run_id=job_run_id)
    if cancelled is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "job run is already terminal")
    job, before_status = cancelled
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="orchestration.job_cancelled",
        target_type="job_run",
        target_id=str(job.id),
        before={"status": before_status},
        after={"status": job.status, "job_key": job.job_key},
        request_id=request.state.request_id,
    )
    await database.commit()
    return await job_response(database, job)


@router.get("/routine-runs", response_model=RoutineRunList)
async def list_routine_runs(
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    page: int = Query(1, ge=1),
) -> RoutineRunList:
    total = await database.scalar(select(func.count()).select_from(RoutineRun)) or 0
    routines = (
        await database.scalars(
            select(RoutineRun)
            .order_by(RoutineRun.created_at.desc(), RoutineRun.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).all()
    responses = await _routine_responses(database, list(routines))
    return RoutineRunList(
        items=responses,
        page=page,
        page_size=PAGE_SIZE,
        total=total,
        has_more=page * PAGE_SIZE < total,
    )


@router.get("/routine-runs/{routine_run_id}", response_model=RoutineRunResponse)
async def get_routine_run(
    routine_run_id: uuid.UUID,
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> RoutineRunResponse:
    routine = await database.get(RoutineRun, routine_run_id)
    if routine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "routine run not found")
    return (await _routine_responses(database, [routine]))[0]


@router.get("/legacy-runs", response_model=LegacyRunList)
async def list_legacy_runs(
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    page: int = Query(1, ge=1),
) -> LegacyRunList:
    total = await database.scalar(select(func.count()).select_from(DataManagementRun)) or 0
    rows = (
        await database.scalars(
            select(DataManagementRun)
            .order_by(DataManagementRun.created_at.desc(), DataManagementRun.id.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).all()
    return LegacyRunList(
        items=[
            LegacyRunResponse(
                id=row.id,
                operation=row.operation,
                market_code=row.market_code,
                edition_date=row.edition_date,
                status=row.status,
                requested_by_user_id=row.requested_by_user_id,
                started_at=row.started_at,
                completed_at=row.completed_at,
                result=row.result,
                error=row.error,
                created_at=row.created_at,
            )
            for row in rows
        ],
        page=page,
        page_size=PAGE_SIZE,
        total=total,
        has_more=page * PAGE_SIZE < total,
    )


async def _routine_responses(
    database: AsyncSession, routines: list[RoutineRun]
) -> list[RoutineRunResponse]:
    if not routines:
        return []
    routine_ids = [routine.id for routine in routines]
    jobs = (
        await database.scalars(
            select(JobRun)
            .where(JobRun.routine_run_id.in_(routine_ids))
            .order_by(JobRun.created_at, JobRun.id)
        )
    ).all()
    job_responses = await _job_responses(database, list(jobs))
    jobs_by_routine: dict[uuid.UUID, list[JobRunResponse]] = {}
    for job, response in zip(jobs, job_responses, strict=True):
        if job.routine_run_id is not None:
            jobs_by_routine.setdefault(job.routine_run_id, []).append(response)
    return [
        RoutineRunResponse(
            id=routine.id,
            routine_key=routine.routine_key,
            registry_version=routine.registry_version,
            edition_date=routine.edition_date,
            scheduled_for=routine.scheduled_for,
            deadline_at=routine.deadline_at,
            status=routine.status,
            started_at=routine.started_at,
            completed_at=routine.completed_at,
            result=routine.result,
            created_at=routine.created_at,
            jobs=jobs_by_routine.get(routine.id, []),
        )
        for routine in routines
    ]


async def job_response(database: AsyncSession, job: JobRun) -> JobRunResponse:
    return (await _job_responses(database, [job]))[0]


async def _job_responses(database: AsyncSession, jobs: list[JobRun]) -> list[JobRunResponse]:
    if not jobs:
        return []
    job_ids = [job.id for job in jobs]
    functions = (
        await database.scalars(
            select(FunctionRun)
            .where(FunctionRun.job_run_id.in_(job_ids))
            .order_by(FunctionRun.created_at, FunctionRun.id)
        )
    ).all()
    dependency_rows = [
        (upstream, downstream)
        for upstream, downstream in (
            await database.execute(
                select(
                    JobDependency.upstream_job_run_id, JobDependency.downstream_job_run_id
                ).where(
                    JobDependency.downstream_job_run_id.in_(job_ids)
                    | JobDependency.upstream_job_run_id.in_(job_ids)
                )
            )
        ).all()
    ]
    function_ids = [function.id for function in functions]
    attempts: list[FunctionAttempt] = []
    function_dependencies: list[tuple[uuid.UUID, uuid.UUID]] = []
    if function_ids:
        ranked_attempts = (
            select(
                FunctionAttempt.id.label("attempt_id"),
                func.row_number()
                .over(
                    partition_by=FunctionAttempt.function_run_id,
                    order_by=FunctionAttempt.attempt_number.desc(),
                )
                .label("attempt_rank"),
            )
            .where(FunctionAttempt.function_run_id.in_(function_ids))
            .subquery()
        )
        attempts = list(
            (
                await database.scalars(
                    select(FunctionAttempt)
                    .join(
                        ranked_attempts,
                        ranked_attempts.c.attempt_id == FunctionAttempt.id,
                    )
                    .where(ranked_attempts.c.attempt_rank <= 100)
                    .order_by(FunctionAttempt.function_run_id, FunctionAttempt.attempt_number)
                )
            ).all()
        )
        function_dependencies = [
            (upstream, downstream)
            for upstream, downstream in (
                await database.execute(
                    select(
                        FunctionDependency.upstream_function_run_id,
                        FunctionDependency.downstream_function_run_id,
                    ).where(FunctionDependency.downstream_function_run_id.in_(function_ids))
                )
            ).all()
        ]
    functions_by_job: dict[uuid.UUID, list[FunctionRun]] = {}
    attempts_by_function: dict[uuid.UUID, list[FunctionAttempt]] = {}
    dependencies_by_function: dict[uuid.UUID, list[uuid.UUID]] = {}
    for function in functions:
        functions_by_job.setdefault(function.job_run_id, []).append(function)
    for attempt in attempts:
        attempts_by_function.setdefault(attempt.function_run_id, []).append(attempt)
    for upstream, downstream in function_dependencies:
        dependencies_by_function.setdefault(downstream, []).append(upstream)
    return [
        _job_response(
            job,
            functions_by_job.get(job.id, []),
            attempts_by_function,
            dependencies_by_function,
            list(dependency_rows),
        )
        for job in jobs
    ]


def _job_response(
    job: JobRun,
    functions: list[FunctionRun],
    attempts_by_function: dict[uuid.UUID, list[FunctionAttempt]],
    dependencies_by_function: dict[uuid.UUID, list[uuid.UUID]],
    dependency_rows: list[tuple[uuid.UUID, uuid.UUID]],
) -> JobRunResponse:
    return JobRunResponse(
        id=job.id,
        routine_run_id=job.routine_run_id,
        job_key=job.job_key,
        kind=job.kind,
        trigger=job.trigger,
        edition_date=job.edition_date,
        deadline_at=job.deadline_at,
        status=job.status,
        requested_by_user_id=job.requested_by_user_id,
        payload=job.payload,
        started_at=job.started_at,
        completed_at=job.completed_at,
        result=job.result,
        error=job.error,
        created_at=job.created_at,
        functions=[
            _function_response(
                function,
                attempts_by_function.get(function.id, []),
                dependencies_by_function.get(function.id, []),
            )
            for function in functions
        ],
        depends_on=[upstream for upstream, downstream in dependency_rows if downstream == job.id],
        downstream_jobs=[
            downstream for upstream, downstream in dependency_rows if upstream == job.id
        ],
    )


def _function_response(
    function: FunctionRun,
    attempts: list[FunctionAttempt],
    dependencies: list[uuid.UUID],
) -> FunctionRunResponse:
    return FunctionRunResponse(
        id=function.id,
        function_key=function.function_key,
        provider_key=function.provider_key,
        scope=function.scope,
        status=function.status,
        missing_scopes=function.missing_scopes,
        attempt_count=function.attempt_count,
        next_attempt_at=function.next_attempt_at,
        started_at=function.started_at,
        completed_at=function.completed_at,
        result=function.result,
        error=function.error,
        depends_on=dependencies,
        attempts=[
            FunctionAttemptResponse(
                id=attempt.id,
                attempt_number=attempt.attempt_number,
                status=attempt.status,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                source_as_of=attempt.source_as_of,
                fetched_at=attempt.fetched_at,
                record_count=attempt.record_count,
                payload_digest=attempt.payload_digest,
                request_metadata=attempt.request_metadata,
                result=attempt.result,
                error_code=attempt.error_code,
                error_detail=attempt.error_detail,
            )
            for attempt in attempts
        ],
    )
