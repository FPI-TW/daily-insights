"""Single import point used by Alembic to register all modular tables."""

from daily_insights_api.modules.analyst_viewpoints.models import (
    AnalystViewpoint,
    AnalystViewpointSyncRun,
    AnalystViewpointVersion,
)
from daily_insights_api.modules.assets.models import (
    Asset,
    AssetMigrationEntry,
    AssetMigrationManifest,
)
from daily_insights_api.modules.audit.models import AuditEvent
from daily_insights_api.modules.chat.models import Conversation, Message
from daily_insights_api.modules.data_management.models import DataManagementRun
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import LoginThrottle, Session
from daily_insights_api.modules.markets.models import Market, OrganizationMarketPolicy
from daily_insights_api.modules.model_runtime.models import (
    ActiveModelConfiguration,
    GenerationRecord,
    ModelConfiguration,
)
from daily_insights_api.modules.news.models import (
    NewsCandidate,
    NewsCandidateBatch,
    NewsCandidatePublication,
    NewsEdition,
    NewsGenerationAudit,
    NewsItem,
    NewsPresentation,
    PreparedNewsItem,
)
from daily_insights_api.modules.operations.models import ReportPipelineRun, SourceRun
from daily_insights_api.modules.orchestration.models import (
    FunctionAttempt,
    FunctionDependency,
    FunctionRun,
    InterestRateObservation,
    InterestRateSeries,
    JobDependency,
    JobRun,
    MarketDailyObservation,
    MarketDailySeries,
    ProjectionInputFreeze,
    ProjectionInputObservation,
    PublicationFunctionAttempt,
    RoutineRun,
)
from daily_insights_api.modules.podcasts.models import (
    PodcastEpisode,
    PodcastEpisodeAudioVariant,
    PodcastEpisodeTranslation,
)
from daily_insights_api.modules.podcasts.upload_models import (
    PodcastUploadBatch,
    PodcastUploadSession,
)
from daily_insights_api.modules.reports.macro_dashboard_models import MacroDashboardSnapshot
from daily_insights_api.modules.reports.models import PublicationSourceRun, ReportPublication
from daily_insights_api.modules.tenancy.models import Membership, Organization

__all__ = [
    "ActiveModelConfiguration",
    "AnalystViewpoint",
    "AnalystViewpointSyncRun",
    "AnalystViewpointVersion",
    "Asset",
    "AssetMigrationEntry",
    "AssetMigrationManifest",
    "AuditEvent",
    "Conversation",
    "DataManagementRun",
    "FunctionAttempt",
    "FunctionDependency",
    "FunctionRun",
    "GenerationRecord",
    "InterestRateObservation",
    "InterestRateSeries",
    "JobDependency",
    "JobRun",
    "LoginThrottle",
    "MacroDashboardSnapshot",
    "Market",
    "MarketDailyObservation",
    "MarketDailySeries",
    "Membership",
    "Message",
    "ModelConfiguration",
    "NewsCandidate",
    "NewsCandidateBatch",
    "NewsCandidatePublication",
    "NewsEdition",
    "NewsGenerationAudit",
    "NewsItem",
    "NewsPresentation",
    "Organization",
    "OrganizationMarketPolicy",
    "PodcastEpisode",
    "PodcastEpisodeAudioVariant",
    "PodcastEpisodeTranslation",
    "PodcastUploadBatch",
    "PodcastUploadSession",
    "PreparedNewsItem",
    "ProjectionInputFreeze",
    "ProjectionInputObservation",
    "PublicationFunctionAttempt",
    "PublicationSourceRun",
    "ReportPipelineRun",
    "ReportPublication",
    "RoutineRun",
    "Session",
    "SourceRun",
    "User",
]
