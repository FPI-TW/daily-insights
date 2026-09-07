"""Shared authorization for market news across HTTP and chat consumers."""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.identity.api import AuthContext
from daily_insights_api.modules.markets.api import visible_market_codes
from daily_insights_api.modules.news.editions import MARKET_NEWS_CODES

_INTERNAL_PREVIEW_ROLES = frozenset({SystemRole.ADMIN, SystemRole.ASSET_MANAGER})


async def visible_news_market_codes(database: AsyncSession, context: AuthContext) -> frozenset[str]:
    """Market news editions the viewer may read; the global digest is always visible."""
    if context.user.system_role in _INTERNAL_PREVIEW_ROLES:
        return frozenset(MARKET_NEWS_CODES)
    if context.organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    visible = await visible_market_codes(database, context.organization_id)
    return frozenset(code for code in MARKET_NEWS_CODES if code in visible)
