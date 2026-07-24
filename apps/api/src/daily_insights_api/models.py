"""Single import point used by Alembic to register all modular tables."""

from daily_insights_api.modules.assets.models import Asset
from daily_insights_api.modules.chat.models import Conversation, Message
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.markets.models import Market, OrganizationMarketPolicy
from daily_insights_api.modules.model_runtime.models import GenerationRecord, ModelConfiguration
from daily_insights_api.modules.tenancy.models import Membership, Organization

__all__ = [
    "Asset",
    "Conversation",
    "GenerationRecord",
    "Market",
    "Membership",
    "Message",
    "ModelConfiguration",
    "Organization",
    "OrganizationMarketPolicy",
    "User",
]
