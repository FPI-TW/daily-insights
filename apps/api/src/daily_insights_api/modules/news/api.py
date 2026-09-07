"""Public contracts for the independent daily-news module."""

from daily_insights_api.modules.news.access import visible_news_market_codes
from daily_insights_api.modules.news.contracts import Locale, NewsStatus
from daily_insights_api.modules.news.editions import GLOBAL_MARKET
from daily_insights_api.modules.news.models import NewsEdition, NewsItem, NewsPresentation

__all__ = [
    "GLOBAL_MARKET",
    "Locale",
    "NewsEdition",
    "NewsItem",
    "NewsPresentation",
    "NewsStatus",
    "visible_news_market_codes",
]
