"""Public contracts and execution helpers for the daily-news module."""

from daily_insights_api.modules.news.access import visible_news_market_codes
from daily_insights_api.modules.news.contracts import Locale, NewsProgress, NewsStatus
from daily_insights_api.modules.news.editions import (
    EDITION_ORDER,
    GLOBAL_MARKET,
    EditionSpec,
    edition_spec,
)
from daily_insights_api.modules.news.failures import NewsFailure, classify_failure
from daily_insights_api.modules.news.feeds import effective_hostnames
from daily_insights_api.modules.news.llm import DeepSeekClient
from daily_insights_api.modules.news.models import (
    NewsCandidate,
    NewsDependencyState,
    NewsEdition,
    NewsItem,
    NewsPresentation,
    NewsWorkflow,
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
    "EDITION_ORDER",
    "GLOBAL_MARKET",
    "PROVIDER_SCOPE",
    "EditionSpec",
    "Locale",
    "NewsCandidate",
    "NewsDependencyState",
    "NewsEdition",
    "NewsExecution",
    "NewsFailure",
    "NewsItem",
    "NewsPresentation",
    "NewsProgress",
    "NewsStatus",
    "NewsWorkflow",
    "automatic_window",
    "classify_failure",
    "cleanup_checkpoints",
    "create_news_client",
    "dependency_failure",
    "edition_spec",
    "effective_hostnames",
    "news_execution",
    "publish_candidates",
    "run_all_editions",
    "run_all_editions_with_outcomes",
    "run_news_edition",
    "visible_news_market_codes",
    "workflow_results",
    "workflow_scope",
]
