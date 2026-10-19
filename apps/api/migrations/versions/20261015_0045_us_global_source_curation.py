"""Curate US/global newsroom sources after Motley Fool / Yahoo sitemap noise.

Revision ID: 20261015_0045
Revises: 20261015_0044

Data only:

- Disable Motley Fool and the low-yield CNBC economy/finance feeds.
- Keep GlobeNewswire / PR Newswire soft-deleted (already non-news).
- Repoint Yahoo Finance from the noisy news sitemap to the working S&P 500
  headline RSS (``finance.yahoo.com/news/rssindex`` returns 404 as of
  2026-10-15).
- Add CNBC Investing, CNBC Technology, Investing.com stocks, TechCrunch AI.
- Re-enable Federal Reserve and ECB press feeds as trust-tier 3 official
  sources (product previously soft-deleted them as non-news; this restores
  them as first-hand macro catalysts).
- Tighten FXStreet and Guardian World title filters.
"""

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261015_0045"
down_revision: str | None = "20261015_0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMESPACE = uuid.UUID("5b0c6d8e-2f4a-4b8e-9a51-6e1f0d3c7a20")

_CNBC = r"^https://www\.cnbc\.com/\d{4}/\d{2}/\d{2}/[a-z0-9-]+\.html$"
_YAHOO = r"^https://finance\.yahoo\.com/.+\.html$"
_INVESTING = r"^https://www\.investing\.com/news/[a-z0-9-]+/.+$"
_TECHCRUNCH = r"^https://techcrunch\.com/\d{4}/\d{2}/\d{2}/[a-z0-9-]+/?$"
_FED = r"^https://www\.federalreserve\.gov/newsevents/pressreleases/[a-z0-9]+\.htm$"
_ECB = r"^https://www\.ecb\.europa\.eu/+press/.+\.en\.html$"

# ``finance.yahoo.com/news/rssindex`` is 404; S&P 500 headline RSS is the
# working Top/Market news endpoint. Third-party hosts are dropped by hostname.
_YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC&region=US&lang=en-US"
_CNBC_INVESTING = "https://www.cnbc.com/id/15839069/device/rss/rss.html"
_CNBC_TECH = "https://www.cnbc.com/id/19854910/device/rss/rss.html"
_INVESTING_STOCKS = "https://www.investing.com/rss/news_25.rss"
_TECHCRUNCH_AI = "https://techcrunch.com/category/artificial-intelligence/feed/"
_FED_PRESS = "https://www.federalreserve.gov/feeds/press_all.xml"
_ECB_PRESS = "https://www.ecb.europa.eu/rss/press.html"

_FXSTREET_EXCLUDE = (
    r"(?i)\b(price forecast|technical analysis|elliott wave|intraday|"
    r"support and resistance|chart|fibonacci|rsi|macd)\b"
)
_GUARDIAN_WORLD_EXCLUDE = (
    r"(?i)\b(football|premier league|cricket|rugby|celebrity|film|movie|"
    r"music|tv review|obituary|recipe|Crossword|quiz)\b"
)

DISABLE_KEYS = (
    "motley-fool-news",
    "cnbc-economy",
    "cnbc-finance",
)
# Already soft-deleted by 0040; keep them out of the pool.
KEEP_SOFT_DELETED = (
    "globenewswire-mna",
    "globenewswire-earnings",
    "prnewswire-financial-services",
)

US_AND_GLOBAL = ["global", "us_equity"]
US_TW_GLOBAL = ["global", "us_equity", "tw_equity"]
US_TW = ["us_equity", "tw_equity"]
GLOBAL = ["global"]
US = ["us_equity"]
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
    poll: int = FAST,
    weight: float = 1.0,
    title_exclude_pattern: str | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid5(_NAMESPACE, key),
        "key": key,
        "name": name,
        "kind": kind,
        "url": url,
        "hostname": hostname,
        "link_pattern": link_pattern,
        "title_exclude_pattern": title_exclude_pattern,
        "markets": markets,
        "language_filter": None,
        "full_text_in_feed": False,
        "options": {},
        "trust_tier": trust_tier,
        "weight": weight,
        "poll_interval_minutes": poll,
        "enabled": True,
        "deleted_at": None,
    }


ADDED: tuple[dict[str, Any], ...] = (
    _source(
        "cnbc-investing",
        "CNBC",
        "rss",
        _CNBC_INVESTING,
        "www.cnbc.com",
        _CNBC,
        US_AND_GLOBAL,
        2,
        poll=FAST,
    ),
    _source(
        "cnbc-tech",
        "CNBC",
        "rss",
        _CNBC_TECH,
        "www.cnbc.com",
        _CNBC,
        US_TW_GLOBAL,
        2,
        poll=FAST,
    ),
    _source(
        "investing-com-stocks",
        "Investing.com",
        "rss",
        _INVESTING_STOCKS,
        "www.investing.com",
        _INVESTING,
        US_AND_GLOBAL,
        2,
        poll=FAST,
    ),
    _source(
        "techcrunch-ai",
        "TechCrunch",
        "rss",
        _TECHCRUNCH_AI,
        "techcrunch.com",
        _TECHCRUNCH,
        US_TW,
        2,
        poll=NORMAL,
    ),
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


def _clear_errors(*keys: str) -> None:
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key.in_(keys), _TABLE.c.deleted_at.is_(None))
        .values(
            next_poll_at=None,
            consecutive_failures=0,
            last_error_code=None,
            last_error_at=None,
            unhealthy_notified_at=None,
        )
    )


def upgrade() -> None:
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key.in_(DISABLE_KEYS), _TABLE.c.deleted_at.is_(None))
        .values(enabled=False, next_poll_at=None)
    )
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key.in_(KEEP_SOFT_DELETED), _TABLE.c.deleted_at.is_(None))
        .values(enabled=False, deleted_at=sa.func.now())
    )
    op.execute(_TABLE.update().where(_TABLE.c.key.in_(KEEP_SOFT_DELETED)).values(enabled=False))

    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "yahoo-finance-news", _TABLE.c.deleted_at.is_(None))
        .values(
            kind="rss",
            url=_YAHOO_RSS,
            hostname="finance.yahoo.com",
            link_pattern=_YAHOO,
            trust_tier=2,
            weight=1.0,
            poll_interval_minutes=FAST,
            enabled=True,
            title_exclude_pattern=(
                r"(?i)Should You Buy|Retire Millionaire|Penny Stocks?|"
                r"Stocks? To (?:Watch|Buy|Own)|PR Newswire|Business Wire|"
                r"GlobeNewswire|Accesswire|EIN Presswire|Size, Share, Growth|"
                r"Forecast to 20|Market Report \d{4}"
            ),
        )
    )

    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "fxstreet-news", _TABLE.c.deleted_at.is_(None))
        .values(title_exclude_pattern=_FXSTREET_EXCLUDE)
    )
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "guardian-world-rss", _TABLE.c.deleted_at.is_(None))
        .values(title_exclude_pattern=_GUARDIAN_WORLD_EXCLUDE)
    )

    # Official press: undelete and enable as tier-3 first-hand macro sources.
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "fed-press")
        .values(
            kind="rss",
            url=_FED_PRESS,
            hostname="www.federalreserve.gov",
            link_pattern=_FED,
            markets=US_AND_GLOBAL,
            trust_tier=3,
            weight=1.0,
            poll_interval_minutes=NORMAL,
            enabled=True,
            deleted_at=None,
            options={},
        )
    )
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "ecb-press")
        .values(
            kind="rss",
            url=_ECB_PRESS,
            hostname="www.ecb.europa.eu",
            link_pattern=_ECB,
            markets=GLOBAL,
            trust_tier=3,
            weight=1.0,
            poll_interval_minutes=NORMAL,
            enabled=True,
            deleted_at=None,
            options={},
        )
    )

    op.execute(
        postgresql.insert(_TABLE).values(list(ADDED)).on_conflict_do_nothing(index_elements=["key"])
    )
    # If the keys already existed from a partial run, force the intended config.
    for row in ADDED:
        op.execute(
            _TABLE.update()
            .where(_TABLE.c.key == row["key"], _TABLE.c.deleted_at.is_(None))
            .values(
                name=row["name"],
                kind=row["kind"],
                url=row["url"],
                hostname=row["hostname"],
                link_pattern=row["link_pattern"],
                markets=row["markets"],
                trust_tier=row["trust_tier"],
                weight=row["weight"],
                poll_interval_minutes=row["poll_interval_minutes"],
                enabled=True,
            )
        )

    _clear_errors(
        "yahoo-finance-news",
        "fxstreet-news",
        "guardian-world-rss",
        "fed-press",
        "ecb-press",
        *(row["key"] for row in ADDED),
    )


def downgrade() -> None:
    added = [row["key"] for row in ADDED]
    has_articles = sa.exists().where(_ARTICLES.c.source_id == _TABLE.c.id)
    op.execute(_TABLE.delete().where(_TABLE.c.key.in_(added), ~has_articles))
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key.in_(added), _TABLE.c.deleted_at.is_(None))
        .values(enabled=False, deleted_at=sa.func.now())
    )
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key.in_(("fed-press", "ecb-press")))
        .values(enabled=False, deleted_at=sa.func.now())
    )
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key.in_(DISABLE_KEYS), _TABLE.c.deleted_at.is_(None))
        .values(enabled=True)
    )
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "yahoo-finance-news", _TABLE.c.deleted_at.is_(None))
        .values(
            kind="news_sitemap",
            url="https://finance.yahoo.com/news-sitemap.xml",
            hostname="finance.yahoo.com",
            link_pattern=(
                r"^https://finance\.yahoo\.com/"
                r"(?:news|[a-z0-9-]+(?:/[a-z0-9-]+)*/articles)/[a-z0-9-]+\.html$"
            ),
            title_exclude_pattern=None,
            poll_interval_minutes=FAST,
        )
    )
    op.execute(
        _TABLE.update().where(_TABLE.c.key == "fxstreet-news").values(title_exclude_pattern=None)
    )
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "guardian-world-rss")
        .values(title_exclude_pattern=None)
    )
