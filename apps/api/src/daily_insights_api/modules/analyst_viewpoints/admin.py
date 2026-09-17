from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.analyst_viewpoints.schemas import (
    AnalystViewpointSyncStatusResponse,
)
from daily_insights_api.modules.analyst_viewpoints.service import (
    latest_sync_execution,
    list_viewpoints,
)
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.identity.api import (
    AuthContext,
    require_csrf_roles,
    require_roles,
)
from daily_insights_api.modules.orchestration.api import (
    JobRunResponse,
    enqueue_manual_job,
    job_response,
)
from daily_insights_api.web.dependencies import get_database_session

TAIPEI = ZoneInfo("Asia/Taipei")
router = APIRouter(prefix="/api/admin/analyst-viewpoints", tags=["administration"])
AdminRead = Annotated[AuthContext, Depends(require_roles(SystemRole.ADMIN))]
AdminWrite = Annotated[AuthContext, Depends(require_csrf_roles(SystemRole.ADMIN))]


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


@router.post("/sync", response_model=JobRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def manually_sync(
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> JobRunResponse:
    job = await enqueue_manual_job(
        database,
        job_key="analyst_viewpoints_refresh",
        requester_id=actor.user.id,
    )
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
