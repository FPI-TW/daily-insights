from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.analyst_viewpoints.schemas import AnalystViewpointResponse
from daily_insights_api.modules.analyst_viewpoints.service import MARKET_MAPPING, list_viewpoints
from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.markets.api import visible_market_codes
from daily_insights_api.web.dependencies import get_database_session

TAIPEI = ZoneInfo("Asia/Taipei")
router = APIRouter(prefix="/api/analyst-viewpoints", tags=["analyst viewpoints"])
Member = Annotated[AuthContext, Depends(require_password_changed)]


@router.get("/today", response_model=list[AnalystViewpointResponse])
async def get_today_viewpoints(
    context: Member,
    request: Request,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[AnalystViewpointResponse]:
    """Return only valid, visible viewpoints persisted for the current Taipei day."""

    if context.organization_id is not None:
        visible = await visible_market_codes(database, context.organization_id)
    elif context.user.system_role in {SystemRole.ADMIN, SystemRole.ASSET_MANAGER}:
        visible = set(MARKET_MAPPING.values())
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    return await list_viewpoints(database, datetime.now(TAIPEI).date(), market_codes=set(visible))
