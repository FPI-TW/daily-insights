"""Public contracts and execution helpers for the daily-news module."""

from daily_insights_api.modules.news.access import visible_news_market_codes
from daily_insights_api.modules.news.contracts import (
    Locale,
    LocalizedSummary,
    NewsProgress,
    NewsStatus,
    Selection,
)
from daily_insights_api.modules.news.editions import (
    EDITION_ORDER,
    GLOBAL_MARKET,
    EditionSpec,
    edition_spec,
)
from daily_insights_api.modules.news.extraction import FetchedCandidate
from daily_insights_api.modules.news.failures import (
    NewsFailure,
    NewsOperationError,
    classify_failure,
)
from daily_insights_api.modules.news.feeds import (
    discover_feed_candidates,
    effective_hostnames,
    feed_client,
)
from daily_insights_api.modules.news.llm import (
    DeepSeekClient,
    ModelCall,
    ModelCallError,
    publishable_selection,
)
from daily_insights_api.modules.news.models import (
    NewsCandidate,
    NewsCandidateBatch,
    NewsCandidatePublication,
    NewsDependencyState,
    NewsEdition,
    NewsGenerationAudit,
    NewsItem,
    NewsPresentation,
    NewsWorkflow,
    PreparedNewsItem,
)
from daily_insights_api.modules.news.prompts import load_selection_criteria
from daily_insights_api.modules.news.recovery import (
    PROVIDER_SCOPE,
    NewsExecution,
    automatic_window,
    cleanup_checkpoints,
    dependency_failure,
    news_execution,
    workflow_results,
    workflow_scope,
)
from daily_insights_api.modules.news.service import (
    DERIVATION_VERSION,
    LOCALES,
    SUMMARY_PROMPT_VERSION,
    TRANSLATION_PROMPT_VERSION,
    _cap_discovery,
    _digest,
    _edition_status,
    _fetch_usable_candidates,
    _limit_candidates,
    _lock_key,
    publish_candidates,
    run_all_editions,
    run_all_editions_with_outcomes,
    run_news_edition,
)


def create_news_client(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
) -> DeepSeekClient:
    """Build the configured model client used by daily-news executions."""
    return DeepSeekClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        selection_criteria=load_selection_criteria(),
    )


__all__ = [
    "DERIVATION_VERSION",
    "EDITION_ORDER",
    "GLOBAL_MARKET",
    "LOCALES",
    "PROVIDER_SCOPE",
    "SUMMARY_PROMPT_VERSION",
    "TRANSLATION_PROMPT_VERSION",
    "DeepSeekClient",
    "EditionSpec",
    "FetchedCandidate",
    "Locale",
    "LocalizedSummary",
    "ModelCall",
    "ModelCallError",
    "NewsCandidate",
    "NewsCandidateBatch",
    "NewsCandidatePublication",
    "NewsDependencyState",
    "NewsEdition",
    "NewsExecution",
    "NewsFailure",
    "NewsGenerationAudit",
    "NewsItem",
    "NewsOperationError",
    "NewsPresentation",
    "NewsProgress",
    "NewsStatus",
    "NewsWorkflow",
    "PreparedNewsItem",
    "Selection",
    "_cap_discovery",
    "_digest",
    "_edition_status",
    "_fetch_usable_candidates",
    "_limit_candidates",
    "_lock_key",
    "automatic_window",
    "classify_failure",
    "cleanup_checkpoints",
    "create_news_client",
    "dependency_failure",
    "discover_feed_candidates",
    "edition_spec",
    "effective_hostnames",
    "feed_client",
    "load_selection_criteria",
    "news_execution",
    "publish_candidates",
    "publishable_selection",
    "run_all_editions",
    "run_all_editions_with_outcomes",
    "run_news_edition",
    "visible_news_market_codes",
    "workflow_results",
    "workflow_scope",
]
