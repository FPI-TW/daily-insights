import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.markets.models import Market, OrganizationMarketPolicy
from daily_insights_api.modules.markets.schemas import MarketResponse


async def market_responses(
    database: AsyncSession,
    organization_id: uuid.UUID,
    *,
    visible_only: bool,
) -> list[MarketResponse]:
    markets = (await database.scalars(select(Market).order_by(Market.code))).all()
    policies = {
        policy.market_code: policy
        for policy in (
            await database.scalars(
                select(OrganizationMarketPolicy).where(
                    OrganizationMarketPolicy.organization_id == organization_id
                )
            )
        ).all()
    }
    responses = []
    for market in markets:
        policy = policies.get(market.code)
        responses.append(
            MarketResponse(
                code=market.code,
                name_en=market.name_en,
                name_zh_hant=market.name_zh_hant,
                name_zh_hans=market.name_zh_hans,
                is_visible=True if policy is None else policy.is_visible,
            )
        )
    return [market for market in responses if market.is_visible] if visible_only else responses
