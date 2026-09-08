from typing import Annotated, cast

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
    IndexYahooRunResponse,
    InstitutionalTwseRunResponse,
    MacroDashboardRunResponse,
    MorningAllRunResponse,
    MorningMarketRunResponse,
    NewsAllRunResponse,
    NewsMarketRunResponse,
    RunOperationGroup,
)
from daily_insights_api.modules.data_management.service import (
    RunAlreadyActiveError,
    cancel_run,
    enqueue_run,
    taipei_today,
)
from daily_insights_api.modules.identity.api import AuthContext, require_csrf_roles, require_roles
from daily_insights_api.modules.news.api import EDITION_ORDER
from daily_insights_api.modules.reports.api import ACTIVE_LAUNCH_MANIFEST
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/admin/data-management", tags=["data management"])
AdminRead = Annotated[AuthContext, Depends(require_roles(SystemRole.ADMIN))]
AdminWrite = Annotated[AuthContext, Depends(require_csrf_roles(SystemRole.ADMIN))]


def response(run: DataManagementRun) -> DataManagementRunResponse:
    values = dict(
        id=run.id,
        edition_date=run.edition_date,
        status=run.status,
        requested_by_user_id=run.requested_by_user_id,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        result=run.result,
        error=run.error,
    )
    if run.operation == "morning_all":
        return MorningAllRunResponse(operation="morning_all", market_code=None, **values)
    if run.operation == "morning_market":
        return MorningMarketRunResponse(
            operation="morning_market",
            market_code=cast(str, run.market_code),
            **values,
        )
    if run.operation == "institutional_twse":
        return InstitutionalTwseRunResponse(
            operation="institutional_twse", market_code=None, **values
        )
    if run.operation == "news_all":
        return NewsAllRunResponse(operation="news_all", market_code=None, **values)
    if run.operation == "news_market":
        return NewsMarketRunResponse(
            operation="news_market", market_code=cast(str, run.market_code), **values
        )
    if run.operation == "macro_dashboard":
        return MacroDashboardRunResponse(operation="macro_dashboard", market_code=None, **values)
    return IndexYahooRunResponse(operation="index_yahoo", market_code=None, **values)


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
        statement = statement.where(DataManagementRun.operation.in_(("news_all", "news_market")))
    runs = (
        await database.scalars(statement.order_by(DataManagementRun.created_at.desc()).limit(limit))
    ).all()
    return DataManagementRunList(items=[response(run) for run in runs])


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
