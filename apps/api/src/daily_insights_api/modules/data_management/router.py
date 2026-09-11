from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.data_management.models import DataManagementRun
from daily_insights_api.modules.data_management.schemas import (
    DataManagementCatalog,
    DataManagementRunCreate,
    DataManagementRunList,
    DataManagementRunResponse,
    NewsResumeRequest,
    RunOperationGroup,
    run_response,
)
from daily_insights_api.modules.data_management.service import (
    RunAlreadyActiveError,
    cancel_run,
    enqueue_run,
    resume_news_run,
    taipei_today,
)
from daily_insights_api.modules.identity.api import AuthContext, require_csrf_roles, require_roles
from daily_insights_api.modules.news.api import EDITION_ORDER, NewsProgress, NewsWorkflow
from daily_insights_api.modules.reports.api import ACTIVE_LAUNCH_MANIFEST
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/admin/data-management", tags=["data management"])
AdminRead = Annotated[AuthContext, Depends(require_roles(SystemRole.ADMIN))]
AdminWrite = Annotated[AuthContext, Depends(require_csrf_roles(SystemRole.ADMIN))]


response = run_response


@router.get("/catalog", response_model=DataManagementCatalog)
async def catalog(request: Request, _: AdminRead) -> DataManagementCatalog:
    settings = request.app.state.settings
    return DataManagementCatalog(
        taipei_date=taipei_today(),
        morning_reports_enabled=settings.morning_reports_enabled,
        yfinance_enabled=settings.yfinance_enabled,
        twse_enabled=settings.twse_enabled,
        markets=[item.market_code for item in ACTIVE_LAUNCH_MANIFEST.markets],
        daily_news_enabled=settings.daily_news_enabled,
        news_markets=list(EDITION_ORDER),
        macro_dashboard_enabled=True,
    )


@router.post(
    "/runs",
    response_model=DataManagementRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_409_CONFLICT: {"description": "An operation class is already active."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Requested provider is unavailable."},
    },
)
async def create_run(
    payload: DataManagementRunCreate,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> DataManagementRunResponse:
    settings = request.app.state.settings
    if payload.operation.startswith("morning") and not settings.morning_reports_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "morning reports are unavailable")
    if payload.operation == "index_yahoo" and not settings.yfinance_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "yfinance is unavailable")
    if payload.operation == "institutional_twse" and not settings.twse_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "twse is unavailable")
    if payload.operation.startswith("news") and not settings.daily_news_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "daily news is unavailable")
    # The historical single-market request remains accepted for compatibility,
    # but Global macro is now the durable dashboard snapshot rather than a
    # separate morning-report target.
    operation = (
        "macro_dashboard"
        if payload.operation == "morning_market" and payload.market_code == "global_macro_bonds"
        else payload.operation
    )
    market_code = None if operation == "macro_dashboard" else payload.market_code
    try:
        run = await enqueue_run(
            database,
            operation=operation,
            market_code=market_code,
            requester_id=actor.user.id,
            request_id=request.state.request_id,
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except RunAlreadyActiveError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "an operation of this class is already active"
        ) from None
    return response(run)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=DataManagementRunResponse,
    responses={
        status.HTTP_409_CONFLICT: {"description": "Run is terminal already or no longer exists."}
    },
)
async def cancel_existing_run(
    run_id: str,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> DataManagementRunResponse:
    import uuid

    try:
        parsed = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from None
    run = await cancel_run(
        database, run_id=parsed, actor_user_id=actor.user.id, request_id=request.state.request_id
    )
    if run is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not pending or running")
    return response(run)


@router.get("/runs", response_model=DataManagementRunList)
async def list_runs(
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    limit: int = Query(default=20, ge=1, le=20),
    operation_group: Annotated[RunOperationGroup | None, Query()] = None,
) -> DataManagementRunList:
    statement = select(DataManagementRun)
    if operation_group == "news":
        statement = statement.where(
            DataManagementRun.operation.in_(("news_all", "news_market", "news_publish"))
        )
    runs = (
        await database.scalars(statement.order_by(DataManagementRun.created_at.desc()).limit(limit))
    ).all()
    return DataManagementRunList(
        items=[response(run, news=await _news_progress(database, run)) for run in runs]
    )


async def _news_progress(
    database: AsyncSession, run: DataManagementRun
) -> dict[str, NewsProgress] | None:
    if not run.operation.startswith("news") or run.status not in {"pending", "running"}:
        return None
    workflows = (
        await database.scalars(select(NewsWorkflow).where(NewsWorkflow.run_id == run.id))
    ).all()
    return {
        row.market_code: NewsProgress(
            id=str(row.id),
            state=row.state,
            stage=row.stage,
            progress=row.progress,
            failures=row.failures,
            attempt=row.attempt,
            next_retry_at=row.next_retry_at,
            publication="technical_degradation"
            if row.failures
            else "editorial_shortfall"
            if row.progress.get("published", 0) < 5
            else "available",
        )
        for row in workflows
    } or None


@router.post(
    "/runs/{run_id}/resume",
    response_model=DataManagementRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_run(
    run_id: str,
    payload: NewsResumeRequest,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> DataManagementRunResponse:
    import uuid

    try:
        parsed = uuid.UUID(run_id)
        run = await resume_news_run(
            database,
            run_id=parsed,
            actor_user_id=actor.user.id,
            request_id=request.state.request_id,
            resume_provider=payload.resume_provider,
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except RunAlreadyActiveError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, "news recovery already active") from error
    return response(run)


@router.get("/runs/{run_id}", response_model=DataManagementRunResponse)
async def get_run(
    run_id: str, _: AdminRead, database: Annotated[AsyncSession, Depends(get_database_session)]
) -> DataManagementRunResponse:
    import uuid

    try:
        parsed = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from None
    run = await database.get(DataManagementRun, parsed)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return response(run, news=await _news_progress(database, run))
