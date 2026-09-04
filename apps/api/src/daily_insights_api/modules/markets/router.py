import uuid
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.data_sources.api import TRACKED_INDICES
from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.markets.api import (
    IndexDailyBarResponse,
    IndexLatestBarResponse,
    MarketResponse,
    index_daily_bars,
    latest_index_bars,
    market_responses,
    visible_market_codes,
)
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/markets", tags=["markets"])

DEFAULT_BARS_WINDOW = timedelta(days=365)
# ~2,500 rows at most per call; a full ^GSPC history would be ~24k.
MAX_BARS_RANGE = timedelta(days=365 * 10)
# Path parameters arrive as str; the Literal-keyed mapping is widened for lookup.
INDEX_MARKETS: dict[str, str] = {symbol: market for symbol, market in TRACKED_INDICES.items()}


def _organization_id(context: AuthContext) -> uuid.UUID:
    if context.organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    return context.organization_id


@router.get("", response_model=list[MarketResponse])
async def list_visible_markets(
    context: Annotated[AuthContext, Depends(require_password_changed)],
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[MarketResponse]:
    return await market_responses(database, _organization_id(context), visible_only=True)


@router.get("/indices", response_model=list[IndexLatestBarResponse])
async def list_index_latest_bars(
    context: Annotated[AuthContext, Depends(require_password_changed)],
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[IndexLatestBarResponse]:
    visible = await visible_market_codes(database, _organization_id(context))
    return await latest_index_bars(database, market_codes=visible)


@router.get("/indices/{symbol}/daily-bars", response_model=list[IndexDailyBarResponse])
async def list_index_daily_bars(
    symbol: str,
    context: Annotated[AuthContext, Depends(require_password_changed)],
    database: Annotated[AsyncSession, Depends(get_database_session)],
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> list[IndexDailyBarResponse]:
    visible = await visible_market_codes(database, _organization_id(context))
    # Unknown and hidden look the same, as /api/reports does: a 404 reveals
    # nothing about which markets an organization's contract excludes.
    if INDEX_MARKETS.get(symbol) not in visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "index not found")
    end = end or date.today()
    start = start or end - DEFAULT_BARS_WINDOW
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "start must not be after end")
    if end - start > MAX_BARS_RANGE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"date range must not exceed {MAX_BARS_RANGE.days // 365} years",
        )
    return await index_daily_bars(database, symbol=symbol, start=start, end=end)
