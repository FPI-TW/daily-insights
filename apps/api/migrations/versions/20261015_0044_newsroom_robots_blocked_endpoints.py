"""Repoint robots-blocked media onto working discovery endpoints.

Revision ID: 20261015_0044
Revises: 20261015_0043

Data only. The admin console showed MarketWatch, Yahoo Finance, Seeking Alpha,
TechNews, The Motley Fool and MoneyDJ failing with ``robots_disallowed``. With
``DAILY_INSIGHTS_NEWS_IGNORE_ROBOTS`` defaulting to true, these publishers are
reachable again via RSS, news sitemaps, or HTML lists (see
``docs/research/robots-blocked-sources-recovery.md``).

Upgrade:

- Inserts the canonical keys when missing (``on_conflict_do_nothing``).
- Overwrites any still-enabled row whose ``name`` matches the English labels
  from the admin list (manual additions), so existing broken URLs are fixed.
- Clears poll-error state and schedules an immediate poll on those rows plus
  the seeded ``technews-finance`` and ``moneydj-mb00`` sources.

Downgrade soft-disables the newly inserted keys (or deletes them when they
still have no articles) and does not restore prior manual URLs.
"""

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261015_0044"
down_revision: str | None = "20261015_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMESPACE = uuid.UUID("5b0c6d8e-2f4a-4b8e-9a51-6e1f0d3c7a20")

_MW_LINK = r"^https://www\.marketwatch\.com/story/[a-z0-9-]+$"
_YAHOO_LINK = (
    r"^https://finance\.yahoo\.com/"
    r"(?:news|[a-z0-9-]+(?:/[a-z0-9-]+)*/articles)/[a-z0-9-]+\.html$"
)
_SA_LINK = r"^https://seekingalpha\.com/news/\d+-[a-z0-9-]+$"
_FOOL_LINK = r"^https://www\.fool\.com/(?:investing|coverage)/.+$"
_MONEYDJ_LINK = r"(?i)^https://www\.moneydj\.com/kmdj/news/newsviewer\.aspx\?a=[0-9a-f-]+"
_TECHNEWS_LINK = r"^https://finance\.technews\.tw/\d{4}/\d{2}/\d{2}/[a-z0-9-]+/$"

US_AND_GLOBAL = ["global", "us_equity"]
US = ["us_equity"]
TAIWAN = ["tw_equity"]
FAST, NORMAL = 15, 30


def _source(
    key: str,
    name: str,
    kind: str,
    url: str,
    hostname: str,
    link_pattern: str,
    markets: list[str],
    trust_tier: int,
    *,
    poll: int = NORMAL,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid5(_NAMESPACE, key),
        "key": key,
        "name": name,
        "kind": kind,
        "url": url,
        "hostname": hostname,
        "link_pattern": link_pattern,
        "markets": markets,
        "language_filter": None,
        "full_text_in_feed": False,
        "options": options or {},
        "trust_tier": trust_tier,
        "weight": 1.0,
        "poll_interval_minutes": poll,
        "enabled": True,
        "deleted_at": None,
    }


ADDED: tuple[dict[str, Any], ...] = (
    _source(
        "marketwatch-topstories",
        "MarketWatch",
        "rss",
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "www.marketwatch.com",
        _MW_LINK,
        US_AND_GLOBAL,
        2,
        poll=FAST,
    ),
    _source(
        "yahoo-finance-news",
        "Yahoo Finance",
        "news_sitemap",
        "https://finance.yahoo.com/news-sitemap.xml",
        "finance.yahoo.com",
        _YAHOO_LINK,
        US_AND_GLOBAL,
        2,
        poll=FAST,
    ),
    _source(
        "seeking-alpha-currents",
        "Seeking Alpha",
        "rss",
        "https://seekingalpha.com/market_currents.xml",
        "seekingalpha.com",
        _SA_LINK,
        US,
        1,
        poll=FAST,
    ),
    _source(
        "motley-fool-news",
        "The Motley Fool",
        "news_sitemap",
        "https://www.fool.com/news-sitemap.xml",
        "www.fool.com",
        _FOOL_LINK,
        US_AND_GLOBAL,
        1,
        poll=FAST,
    ),
)

# English admin labels from the robots_disallowed list; rows already under a
# canonical key are left to the insert path.
_NAME_TARGETS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "MarketWatch",
        {
            "kind": "rss",
            "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
            "hostname": "www.marketwatch.com",
            "link_pattern": _MW_LINK,
            "options": {},
            "poll_interval_minutes": FAST,
            "enabled": True,
        },
    ),
    (
        "Yahoo Finance",
        {
            "kind": "news_sitemap",
            "url": "https://finance.yahoo.com/news-sitemap.xml",
            "hostname": "finance.yahoo.com",
            "link_pattern": _YAHOO_LINK,
            "options": {},
            "poll_interval_minutes": FAST,
            "enabled": True,
        },
    ),
    (
        "Seeking Alpha",
        {
            "kind": "rss",
            "url": "https://seekingalpha.com/market_currents.xml",
            "hostname": "seekingalpha.com",
            "link_pattern": _SA_LINK,
            "options": {},
            "poll_interval_minutes": FAST,
            "enabled": True,
        },
    ),
    (
        "The Motley Fool",
        {
            "kind": "news_sitemap",
            "url": "https://www.fool.com/news-sitemap.xml",
            "hostname": "www.fool.com",
            "link_pattern": _FOOL_LINK,
            "options": {},
            "poll_interval_minutes": FAST,
            "enabled": True,
        },
    ),
    (
        "TechNews",
        {
            "kind": "rss",
            "url": "https://cdn.technews.tw/feed/",
            "hostname": "finance.technews.tw",
            "link_pattern": _TECHNEWS_LINK,
            "options": {},
            "poll_interval_minutes": FAST,
            "enabled": True,
        },
    ),
    (
        "MoneyDJ",
        {
            "kind": "html_list",
            "url": "https://www.moneydj.com/KMDJ/News/NewsRealList.aspx?a=MB00",
            "hostname": "www.moneydj.com",
            "link_pattern": _MONEYDJ_LINK,
            "options": {"href_contains": "NewsViewer.aspx", "keep_query": True},
            "poll_interval_minutes": FAST,
            "enabled": True,
        },
    ),
)

_CANONICAL_KEYS = (
    *(source["key"] for source in ADDED),
    "technews-finance",
    "moneydj-mb00",
)

_TABLE = sa.table(
    "newsroom_sources",
    sa.column("id", sa.UUID()),
    sa.column("key", sa.String()),
    sa.column("name", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("url", sa.Text()),
    sa.column("hostname", sa.String()),
    sa.column("link_pattern", sa.String()),
    sa.column("title_exclude_pattern", sa.String()),
    sa.column("markets", postgresql.ARRAY(sa.String())),
    sa.column("language_filter", postgresql.ARRAY(sa.String())),
    sa.column("full_text_in_feed", sa.Boolean()),
    sa.column("options", postgresql.JSONB()),
    sa.column("trust_tier", sa.Integer()),
    sa.column("weight", sa.Float()),
    sa.column("poll_interval_minutes", sa.Integer()),
    sa.column("enabled", sa.Boolean()),
    sa.column("deleted_at", sa.DateTime(timezone=True)),
    sa.column("next_poll_at", sa.DateTime(timezone=True)),
    sa.column("consecutive_failures", sa.Integer()),
    sa.column("last_error_code", sa.String()),
    sa.column("last_error_at", sa.DateTime(timezone=True)),
    sa.column("unhealthy_notified_at", sa.DateTime(timezone=True)),
)
_ARTICLES = sa.table("newsroom_articles", sa.column("source_id", sa.UUID()))


def _clear_poll_state(*keys: str) -> None:
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key.in_(keys), _TABLE.c.deleted_at.is_(None))
        .values(
            next_poll_at=None,
            consecutive_failures=0,
            last_error_code=None,
            last_error_at=None,
            unhealthy_notified_at=None,
            enabled=True,
        )
    )


def upgrade() -> None:
    # Fix manually added English-named rows first, then ensure the canonical
    # keys exist. Leaving both enabled is fine; the admin can delete duplicates.
    for name, values in _NAME_TARGETS:
        op.execute(
            _TABLE.update()
            .where(_TABLE.c.name == name, _TABLE.c.deleted_at.is_(None))
            .values(**values)
        )
    op.execute(
        postgresql.insert(_TABLE).values(list(ADDED)).on_conflict_do_nothing(index_elements=["key"])
    )
    # Keep the seeded TechNews and MoneyDJ rows on the working endpoints too.
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "technews-finance", _TABLE.c.deleted_at.is_(None))
        .values(
            kind="rss",
            url="https://cdn.technews.tw/feed/",
            hostname="finance.technews.tw",
            link_pattern=_TECHNEWS_LINK,
            enabled=True,
        )
    )
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "moneydj-mb00", _TABLE.c.deleted_at.is_(None))
        .values(
            kind="html_list",
            url="https://www.moneydj.com/KMDJ/News/NewsRealList.aspx?a=MB00",
            hostname="www.moneydj.com",
            link_pattern=_MONEYDJ_LINK,
            options={"href_contains": "NewsViewer.aspx", "keep_query": True},
            poll_interval_minutes=FAST,
            enabled=True,
        )
    )
    _clear_poll_state(*_CANONICAL_KEYS)
    op.execute(
        _TABLE.update()
        .where(
            _TABLE.c.name.in_([name for name, _ in _NAME_TARGETS]),
            _TABLE.c.deleted_at.is_(None),
        )
        .values(
            next_poll_at=None,
            consecutive_failures=0,
            last_error_code=None,
            last_error_at=None,
            unhealthy_notified_at=None,
        )
    )


def downgrade() -> None:
    added = [source["key"] for source in ADDED]
    has_articles = sa.exists().where(_ARTICLES.c.source_id == _TABLE.c.id)
    op.execute(_TABLE.delete().where(_TABLE.c.key.in_(added), ~has_articles))
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key.in_(added), _TABLE.c.deleted_at.is_(None))
        .values(enabled=False, deleted_at=sa.func.now())
    )
