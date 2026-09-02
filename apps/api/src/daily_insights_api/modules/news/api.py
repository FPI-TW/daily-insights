"""Public contracts for the independent daily-news module."""

from daily_insights_api.modules.news.contracts import Locale, NewsStatus
from daily_insights_api.modules.news.models import NewsEdition, NewsItem, NewsPresentation

__all__ = ["Locale", "NewsEdition", "NewsItem", "NewsPresentation", "NewsStatus"]
