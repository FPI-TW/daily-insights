from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.data_management.models import DataManagementRun
from daily_insights_api.modules.data_management.schemas import (
    DataManagementCatalog,
    DataManagementRunCreate,
    DataManagementRunList,
    DataManagementRunResponse,
    run_response,
)
from daily_insights_api.modules.data_management.service import (
    RunAlreadyActiveError,
    cancel_run,
    enqueue_run,
    taipei_today,
)
from daily_insights_api.modules.identity.api import AuthContext, require_csrf_roles, require_roles
from daily_insights_api.modules.reports.api import ACTIVE_LAUNCH_MANIFEST
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/admin/data-management", tags=["data management"])
RUN_PAGE_SIZE = 10
RERUNNABLE_PROVIDERS = ("twelve_data", "yahoo_finance", "twse")
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
        rerunnable_providers=list(RERUNNABLE_PROVIDERS),
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
    if payload.operation == "morning_all" and not (
        settings.morning_reports_enabled and settings.yfinance_enabled and settings.twse_enabled
    ):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "one or more morning-report providers are unavailable",
        )
    if payload.operation == "provider_rerun":
        if payload.provider == "twelve_data" and not settings.morning_reports_enabled:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "morning reports are unavailable"
            )
        if payload.provider == "yahoo_finance" and not settings.yfinance_enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "yfinance is unavailable")
        if payload.provider == "twse" and not settings.twse_enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "twse is unavailable")
    # The database column remains named market_code for compatibility with
    # historical queue rows; provider reruns store their provider code there.
    operation = payload.operation
    market_code = (
        payload.provider
        if payload.operation == "provider_rerun"
        else None
        if operation == "macro_dashboard"
        else payload.market_code
    )
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
    page: int = Query(default=1, ge=1),
) -> DataManagementRunList:
    total = await database.scalar(select(func.count()).select_from(DataManagementRun)) or 0
    runs = (
        await database.scalars(
            select(DataManagementRun)
            .order_by(DataManagementRun.created_at.desc(), DataManagementRun.id.desc())
            .offset((page - 1) * RUN_PAGE_SIZE)
            .limit(RUN_PAGE_SIZE)
        )
    ).all()
    active_runs = (
        await database.scalars(
            select(DataManagementRun)
            .where(
                or_(
                    DataManagementRun.status.in_(("pending", "running")),
                    and_(
                        DataManagementRun.status == "cancelled",
                        DataManagementRun.lease_owner.is_not(None),
                    ),
                ),
            )
            .order_by(DataManagementRun.created_at.desc(), DataManagementRun.id.desc())
        )
    ).all()
    return DataManagementRunList(
        items=[response(run) for run in runs],
        page=page,
        total=total,
        has_more=page * RUN_PAGE_SIZE < total,
        active_runs=[response(run) for run in active_runs],
    )


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
    return response(run)
