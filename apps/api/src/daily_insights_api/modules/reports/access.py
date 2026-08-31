from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.identity.api import AuthContext
from daily_insights_api.modules.markets.api import visible_market_codes
from daily_insights_api.modules.reports.launch_manifest import LAUNCH_MARKET_ORDER

_INTERNAL_PREVIEW_ROLES = frozenset({SystemRole.ADMIN, SystemRole.ASSET_MANAGER})


async def visible_report_market_codes(
    database: AsyncSession,
    context: AuthContext,
) -> frozenset[str]:
    """Return the formal launch markets available to the authenticated viewer."""
    if context.user.system_role in _INTERNAL_PREVIEW_ROLES:
        return frozenset(LAUNCH_MARKET_ORDER)
    if context.organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    return frozenset(await visible_market_codes(database, context.organization_id))
