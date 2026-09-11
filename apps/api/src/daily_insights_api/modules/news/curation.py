"""Keep administrator suppression effective across revisions and fallbacks."""

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from daily_insights_api.modules.news.models import NewsEdition, NewsItem


def visible_item(market_code: str) -> ColumnElement[bool]:
    hidden = aliased(NewsItem)
    edition = aliased(NewsEdition)
    suppressed = (
        select(hidden.id)
        .join(edition, hidden.edition_id == edition.id)
        .where(
            edition.market_code == market_code,
            hidden.hidden_at.is_not(None),
            or_(
                hidden.source_url == NewsItem.source_url,
                and_(hidden.event_key.is_not(None), hidden.event_key == NewsItem.event_key),
            ),
        )
        .correlate(NewsItem)
        .exists()
    )
    return and_(NewsItem.hidden_at.is_(None), ~suppressed)
