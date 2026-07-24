from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.markets.api import MarketResponse, market_responses
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/markets", tags=["markets"])


@router.get("", response_model=list[MarketResponse])
async def list_visible_markets(
    context: Annotated[AuthContext, Depends(require_password_changed)],
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[MarketResponse]:
    if context.organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    return await market_responses(database, context.organization_id, visible_only=True)
