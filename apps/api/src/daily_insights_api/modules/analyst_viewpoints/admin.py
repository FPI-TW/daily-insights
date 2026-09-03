from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.analyst_viewpoints.schemas import (
    AnalystViewpointSyncResponse,
    AnalystViewpointSyncStatusResponse,
)
from daily_insights_api.modules.analyst_viewpoints.service import (
    AnalystViewpointClient,
    AnalystViewpointSyncError,
    latest_sync_execution,
    list_viewpoints,
    record_sync_execution,
    sync_viewpoints,
)
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.identity.api import (
    AuthContext,
    require_csrf_roles,
    require_roles,
)
from daily_insights_api.web.dependencies import get_database_session

TAIPEI = ZoneInfo("Asia/Taipei")
router = APIRouter(prefix="/api/admin/analyst-viewpoints", tags=["administration"])
AdminRead = Annotated[AuthContext, Depends(require_roles(SystemRole.ADMIN))]
AdminWrite = Annotated[AuthContext, Depends(require_csrf_roles(SystemRole.ADMIN))]


def _client(request: Request) -> AnalystViewpointClient:
    settings = request.app.state.settings
    if not settings.analyst_viewpoints_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "analyst viewpoint sync is disabled")
    if settings.analyst_viewpoints_api_key is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "analyst viewpoint sync is unavailable",
        )
    return AnalystViewpointClient(
        base_url=settings.analyst_viewpoints_base_url,
        api_key=settings.analyst_viewpoints_api_key,
        timeout_seconds=settings.analyst_viewpoints_timeout_seconds,
    )


@router.get("/status", response_model=AnalystViewpointSyncStatusResponse)
async def get_sync_status(
    request: Request,
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> AnalystViewpointSyncStatusResponse:
    today = datetime.now(TAIPEI).date()
    return AnalystViewpointSyncStatusResponse(
        enabled=request.app.state.settings.analyst_viewpoints_enabled,
        today=today,
        viewpoints=await list_viewpoints(database, today),
        latest_sync=await latest_sync_execution(database),
    )


@router.post("/sync", response_model=AnalystViewpointSyncResponse)
async def manually_sync(
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> AnalystViewpointSyncResponse:
    today = datetime.now(TAIPEI).date()
    try:
        result = await sync_viewpoints(database, _client(request), today)
    except AnalystViewpointSyncError as error:
        await record_sync_execution(
            database,
            viewpoint_date=today,
            trigger="manual",
            error_code=error.code,
        )
        record_audit_event(
            database,
            actor_user_id=actor.user.id,
            action="analyst_viewpoints.sync.failed",
            target_type="analyst_viewpoints",
            target_id=today.isoformat(),
            after={
                "viewpoint_date": today.isoformat(),
                "fetched_at": None,
                "status": "failed",
                "markets": [],
            },
            reason=type(error).__name__,
            request_id=request.state.request_id,
        )
        await database.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
    await record_sync_execution(
        database,
        viewpoint_date=today,
        trigger="manual",
        result=result,
    )
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="analyst_viewpoints.sync.completed",
        target_type="analyst_viewpoints",
        target_id=today.isoformat(),
        after=result.model_dump(mode="json"),
        request_id=request.state.request_id,
    )
    await database.commit()
    return result
