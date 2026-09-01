"""Single import point used by Alembic to register all modular tables."""

from daily_insights_api.modules.assets.models import (
    Asset,
    AssetMigrationEntry,
    AssetMigrationManifest,
)
from daily_insights_api.modules.audit.models import AuditEvent
from daily_insights_api.modules.chat.models import Conversation, Message
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import LoginThrottle, Session
from daily_insights_api.modules.markets.models import Market, OrganizationMarketPolicy
from daily_insights_api.modules.model_runtime.models import (
    ActiveModelConfiguration,
    GenerationRecord,
    ModelConfiguration,
)
from daily_insights_api.modules.news.models import (
    NewsEdition,
    NewsGenerationAudit,
    NewsItem,
    NewsPresentation,
)
from daily_insights_api.modules.operations.models import ReportPipelineRun, SourceRun
from daily_insights_api.modules.podcasts.models import (
    PodcastEpisode,
    PodcastEpisodeAudioVariant,
    PodcastEpisodeTranslation,
)
from daily_insights_api.modules.reports.models import PublicationSourceRun, ReportPublication
from daily_insights_api.modules.tenancy.models import Membership, Organization

__all__ = [
    "ActiveModelConfiguration",
    "Asset",
    "AssetMigrationEntry",
    "AssetMigrationManifest",
    "AuditEvent",
    "Conversation",
    "GenerationRecord",
    "LoginThrottle",
    "Market",
    "Membership",
    "Message",
    "ModelConfiguration",
    "NewsEdition",
    "NewsGenerationAudit",
    "NewsItem",
    "NewsPresentation",
    "Organization",
    "OrganizationMarketPolicy",
    "PodcastEpisode",
    "PodcastEpisodeAudioVariant",
    "PodcastEpisodeTranslation",
    "PublicationSourceRun",
    "ReportPipelineRun",
    "ReportPublication",
    "Session",
    "SourceRun",
    "User",
]
