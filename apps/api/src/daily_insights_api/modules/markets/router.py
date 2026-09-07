import uuid
from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.data_sources.api import TRACKED_INDICES
from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.markets.api import (
    INSTITUTIONAL_MARKET_CODE,
    IndexDailyBarResponse,
    IndexLatestBarResponse,
    IndexMovingAveragesResponse,
    InstitutionalMarketFlowResponse,
    InstitutionalStockFlowLeadersResponse,
    MarketResponse,
    index_daily_bars,
    index_moving_averages,
    institutional_market_flows,
    institutional_stock_flow_leaders,
    latest_index_bars,
    market_responses,
    visible_market_codes,
)
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/markets", tags=["markets"])

# Every other "today" in this service is Taipei's, and no deployment sets TZ, so
# the process runs in UTC. A naive today() would put the default window up to
# eight hours behind the rest of the product for the first eight hours of each
# Taipei day.
TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_BARS_WINDOW = timedelta(days=365)
# ~2,500 rows at most per call; a full ^GSPC history would be ~24k.
MAX_BARS_RANGE_YEARS = 10
# Path parameters arrive as str; the Literal-keyed mapping is widened for lookup.
INDEX_MARKETS: dict[str, str] = {symbol: market for symbol, market in TRACKED_INDICES.items()}
INTERNAL_PREVIEW_ROLES = frozenset({SystemRole.ADMIN, SystemRole.ASSET_MANAGER})


def _earliest_allowed_start(end: date) -> date:
    """The oldest start a request ending on `end` may ask for.

    Counted in calendar years rather than 365-day steps. Ten calendar years
    span 3,652 or 3,653 days depending on how many leap days they contain, so
    a day count would reject exactly the decade the error message offers.
    """
    if end.year - MAX_BARS_RANGE_YEARS < date.min.year:
        return date.min
    try:
        return end.replace(year=end.year - MAX_BARS_RANGE_YEARS)
    except ValueError:
        # 29 February has no counterpart in a common year.
        return end.replace(year=end.year - MAX_BARS_RANGE_YEARS, day=28)


def _organization_id(context: AuthContext) -> uuid.UUID:
    if context.organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    return context.organization_id


def resolve_index_date_range(start: date | None, end: date | None) -> tuple[date, date]:
    """Apply the shared daily-bar history window contract."""
    resolved_end = end or datetime.now(TAIPEI).date()
    if start is None:
        resolved_start = resolved_end - min(DEFAULT_BARS_WINDOW, resolved_end - date.min)
    else:
        resolved_start = start
    if resolved_start > resolved_end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "start must not be after end")
    if resolved_start < _earliest_allowed_start(resolved_end):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"date range must not exceed {MAX_BARS_RANGE_YEARS} years",
        )
    return resolved_start, resolved_end


async def _readable_index_market(database: AsyncSession, context: AuthContext, symbol: str) -> str:
    visible = await _readable_index_markets(database, context)
    expected_market = INDEX_MARKETS.get(symbol)
    if expected_market is None or expected_market not in visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "index not found")
    return expected_market


async def _readable_index_markets(
    database: AsyncSession,
    context: AuthContext,
) -> set[str]:
    """Which markets' indices this viewer may read.

    Internal staff belong to no organization, so a membership check alone would
    lock them out of their own product. reports/access.py already grants them a
    preview; this is the same rule scoped to the markets that have indices.
    """
    if context.user.system_role in INTERNAL_PREVIEW_ROLES:
        return set(INDEX_MARKETS.values())
    return await visible_market_codes(database, _organization_id(context))


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
    visible = await _readable_index_markets(database, context)
    return await latest_index_bars(database, market_codes=visible)


@router.get("/indices/{symbol}/daily-bars", response_model=list[IndexDailyBarResponse])
async def list_index_daily_bars(
    symbol: str,
    context: Annotated[AuthContext, Depends(require_password_changed)],
    database: Annotated[AsyncSession, Depends(get_database_session)],
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> list[IndexDailyBarResponse]:
    # Unknown and hidden look the same, as /api/reports does: a 404 reveals
    # nothing about which markets an organization's contract excludes.
    expected_market = await _readable_index_market(database, context, symbol)
    start, end = resolve_index_date_range(start, end)
    return await index_daily_bars(
        database,
        symbol=symbol,
        market_code=expected_market,
        start=start,
        end=end,
    )


@router.get("/indices/{symbol}/moving-averages", response_model=IndexMovingAveragesResponse)
async def get_index_moving_averages(
    symbol: str,
    context: Annotated[AuthContext, Depends(require_password_changed)],
    database: Annotated[AsyncSession, Depends(get_database_session)],
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> IndexMovingAveragesResponse:
    expected_market = await _readable_index_market(database, context, symbol)
    start, end = resolve_index_date_range(start, end)
    return await index_moving_averages(
        database,
        symbol=symbol,
        market_code=expected_market,
        start=start,
        end=end,
    )


async def _readable_institutional_market(database: AsyncSession, context: AuthContext) -> str:
    """Same rule as the index routes: internal staff preview, everyone else
    reads what their organization's contract makes visible, and a market they
    cannot see is indistinguishable from one that does not exist."""
    if context.user.system_role not in INTERNAL_PREVIEW_ROLES:
        visible = await visible_market_codes(database, _organization_id(context))
        if INSTITUTIONAL_MARKET_CODE not in visible:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "market not found")
    return INSTITUTIONAL_MARKET_CODE


@router.get("/institutional/market-flows", response_model=list[InstitutionalMarketFlowResponse])
async def list_institutional_market_flows(
    context: Annotated[AuthContext, Depends(require_password_changed)],
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[InstitutionalMarketFlowResponse]:
    market_code = await _readable_institutional_market(database, context)
    return await institutional_market_flows(database, market_code=market_code)


@router.get("/institutional/stock-flows", response_model=InstitutionalStockFlowLeadersResponse)
async def get_institutional_stock_flow_leaders(
    context: Annotated[AuthContext, Depends(require_password_changed)],
    database: Annotated[AsyncSession, Depends(get_database_session)],
    trade_date: Annotated[date | None, Query()] = None,
) -> InstitutionalStockFlowLeadersResponse:
    market_code = await _readable_institutional_market(database, context)
    return await institutional_stock_flow_leaders(
        database, market_code=market_code, trade_date=trade_date
    )
