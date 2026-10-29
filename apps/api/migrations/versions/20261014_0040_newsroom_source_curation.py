"""Curate the seeded sources after ten days of collection (2026-10-04 to 10-14).

Revision ID: 20261014_0040
Revises: 20261014_0039

Data only; every figure below is from the local collection of that period.
The product collects news media only, so sources that are not newsrooms go.

Removed (soft delete: ``deleted_at`` set, disabled, articles kept):

- ``fed-press``, ``ecb-press``, ``sec-8k``: official releases and filings, not news.
- ``prnewswire-financial-services``, ``globenewswire-mna``,
  ``globenewswire-earnings``: company press releases at trust tier 3; 996
  articles led to 2 published events.

Stopped (``enabled = false``), news media of too little value:

- ``storm``: its sitemap refreshes once a day, so articles arrived a median
  14.7 hours late and reopened stories earlier editions had carried.
- ``gvm``, ``cityam``, ``thestreet``: 850 articles led to 10 published events.

Weighted 1.2: ``cna-finance`` and ``cnbc-world``, the most productive wire and
broadcaster feeds. At 1.2 a story stays below the next impact level of an
unweighted source.

Filtered: ``fxstreet-news`` drops its price-forecast pieces and its
"<topic>: <view> - <bank>" institutional notes (378 of 852 articles, all
commentary; the one published event made only of them repeated an earlier
edition's story).

Repointed, only where the row still holds its seeded value:

- ``cna-finance`` reads CNA's own Atom feed instead of feedburner, which
  refreshed about every two hours and, keeping only 20 items, lost roughly 65%
  of CNA's business stories. The Atom feed covers every desk, so the link
  pattern keeps business (``afe``) and technology (``ait``).
  ``cna-finance-crosslist`` keeps reading feedburner for the international and
  cross-strait stories CNA's editors cross-list there.
- ``ltn-business`` reads the business feed: the all-desks feed held 40 items
  covering 27 minutes, so one missed poll lost stories.
- ``guardian-world-rss``, ``guardian-business-rss`` and ``businesstoday`` get
  link patterns that drop their off-topic sections (37-60% of their irrelevant
  articles, none that belonged to a published event). Guardian World also drops
  quote-led features (titles opening with a left single quotation mark).

Added: 工商時報 (Commercial Times) securities and industry live feeds, a
mainstream paper of record for Taiwan markets. Its feed times carry no zone
(Taipei time); about two thirds of its stories are also covered by the existing
papers, the rest are mostly smaller listed companies.

The downgrade restores the seeded values and removes the added sources, or
disables any that already have articles.
"""

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261014_0040"
down_revision: str | None = "20261014_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same namespace as the seed (20261001_0032), so ids stay derivable from keys.
_NAMESPACE = uuid.UUID("5b0c6d8e-2f4a-4b8e-9a51-6e1f0d3c7a20")

REMOVED = (
    "fed-press",
    "ecb-press",
    "sec-8k",
    "prnewswire-financial-services",
    "globenewswire-mna",
    "globenewswire-earnings",
)
STOPPED = ("storm", "gvm", "cityam", "thestreet")
WEIGHTED = ("cna-finance", "cnbc-world")
WEIGHT = 1.2

_GUARDIAN = r"^https://www\.theguardian\.com/[a-z-]+(/[a-z-]+)?/\d{4}/[a-z]{3}/\d{2}/[a-z0-9-]+$"
_GUARDIAN_WORLD = (
    r"^https://www\.theguardian\.com/"
    r"(world|us-news|business|environment|technology|politics|money|global)"
    r"(/live)?/\d{4}/[a-z]{3}/\d{2}/[a-z0-9-]+$"
)
_GUARDIAN_BUSINESS = (
    r"^https://www\.theguardian\.com/"
    r"(?!(?:commentisfree|football|film|society|media|food|lifeandstyle|artanddesign"
    r"|tv-and-radio|education|fashion|sport|music|books|stage|culture)/)"
    r"[a-z-]+(/[a-z-]+)?/\d{4}/[a-z]{3}/\d{2}/[a-z0-9-]+$"
)
# A left single quotation mark opens the Guardian's quote-led features.
_GUARDIAN_QUOTE_LED = "^\N{LEFT SINGLE QUOTATION MARK}"
_BUSINESSTODAY = r"^https://www\.businesstoday\.com\.tw/article/category/\d+/post/\d+/$"
# Politics, lifestyle, travel, health and local-government promotion categories.
_BUSINESSTODAY_ON_TOPIC = (
    r"^https://www\.businesstoday\.com\.tw/article/category/"
    r"(?!(?:183027|183030|183031|183029|249138)/)\d+/post/\d+/$"
)
_CNA_FEEDBURNER = "https://feeds.feedburner.com/rsscna/finance"
_CNA_ATOM = "https://www.cna.com.tw/atomfeed_cfp.xml"
_CNA_ANY_DESK = r"^https://www\.cna\.com\.tw/news/[a-z]+/\d+\.aspx$"
_CNA_BUSINESS = r"^https://www\.cna\.com\.tw/news/(afe|ait)/\d+\.aspx$"
_CNA_CROSSLISTED = r"^https://www\.cna\.com\.tw/news/(aopl|acn)/\d+\.aspx$"
_CTEE = r"^https://www\.ctee\.com\.tw/news/\d{14}-\d+$"
_LTN_ALL = "https://news.ltn.com.tw/rss/all.xml"
_LTN_BUSINESS = "https://news.ltn.com.tw/rss/business.xml"
# Price forecasts, and bank notes titled "<topic>: <view> - <bank>".
_FXSTREET_COMMENTARY = (
    r"Price (?:Forecast|Analysis|Prediction)"
    "| [\N{EN DASH}-] [A-Z][A-Za-z&. ]{1,30}$"
)

# (key, column, seeded value, new value); applied only while the seeded value stands.
REPOINTED: tuple[tuple[str, str, str, str], ...] = (
    ("guardian-world-rss", "link_pattern", _GUARDIAN, _GUARDIAN_WORLD),
    ("guardian-business-rss", "link_pattern", _GUARDIAN, _GUARDIAN_BUSINESS),
    ("businesstoday", "link_pattern", _BUSINESSTODAY, _BUSINESSTODAY_ON_TOPIC),
    ("ltn-business", "url", _LTN_ALL, _LTN_BUSINESS),
)


def _added(
    key: str,
    name: str,
    kind: str,
    url: str,
    hostname: str,
    link_pattern: str,
    markets: list[str],
    trust_tier: int,
    poll: int,
    weight: float = 1.0,
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
        "weight": weight,
        "poll_interval_minutes": poll,
        "enabled": True,
    }


ADDED: tuple[dict[str, Any], ...] = (
    _added(
        "cna-finance-crosslist",
        "中央社",
        "rss",
        _CNA_FEEDBURNER,
        "www.cna.com.tw",
        _CNA_CROSSLISTED,
        ["tw_equity"],
        2,
        30,
        weight=WEIGHT,
    ),
    *(
        _added(
            f"ctee-{desk}",
            "工商時報",
            "rss",
            f"https://www.ctee.com.tw/rss_web/livenews/{desk}",
            "www.ctee.com.tw",
            _CTEE,
            ["tw_equity"],
            2,
            15,
            options={"naive_time_zone": "Asia/Taipei"},
        )
        for desk in ("stock", "industry")
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
)
_ARTICLES = sa.table("newsroom_articles", sa.column("source_id", sa.UUID()))


def _swap(key: str, column: str, current: str, replacement: str) -> None:
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == key, _TABLE.c[column] == current)
        .values({column: replacement})
    )


def upgrade() -> None:
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key.in_(REMOVED), _TABLE.c.deleted_at.is_(None))
        .values(enabled=False, deleted_at=sa.func.now())
    )
    op.execute(_TABLE.update().where(_TABLE.c.key.in_(STOPPED)).values(enabled=False))
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "fxstreet-news", _TABLE.c.title_exclude_pattern.is_(None))
        .values(title_exclude_pattern=_FXSTREET_COMMENTARY)
    )
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key.in_(WEIGHTED), _TABLE.c.weight == 1.0)
        .values(weight=WEIGHT)
    )
    for key, column, seeded, replacement in REPOINTED:
        _swap(key, column, seeded, replacement)
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "guardian-world-rss", _TABLE.c.title_exclude_pattern.is_(None))
        .values(title_exclude_pattern=_GUARDIAN_QUOTE_LED)
    )
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "cna-finance", _TABLE.c.url == _CNA_FEEDBURNER)
        .values(kind="atom", url=_CNA_ATOM, link_pattern=_CNA_BUSINESS, poll_interval_minutes=10)
    )
    op.execute(
        postgresql.insert(_TABLE).values(list(ADDED)).on_conflict_do_nothing(index_elements=["key"])
    )


def downgrade() -> None:
    added = [source["key"] for source in ADDED]
    has_articles = sa.exists().where(_ARTICLES.c.source_id == _TABLE.c.id)
    op.execute(_TABLE.delete().where(_TABLE.c.key.in_(added), ~has_articles))
    op.execute(_TABLE.update().where(_TABLE.c.key.in_(added)).values(enabled=False))
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key == "cna-finance", _TABLE.c.url == _CNA_ATOM)
        .values(
            kind="rss", url=_CNA_FEEDBURNER, link_pattern=_CNA_ANY_DESK, poll_interval_minutes=15
        )
    )
    op.execute(
        _TABLE.update()
        .where(
            _TABLE.c.key == "guardian-world-rss",
            _TABLE.c.title_exclude_pattern == _GUARDIAN_QUOTE_LED,
        )
        .values(title_exclude_pattern=None)
    )
    for key, column, seeded, replacement in REPOINTED:
        _swap(key, column, replacement, seeded)
    op.execute(
        _TABLE.update()
        .where(_TABLE.c.key.in_(WEIGHTED), _TABLE.c.weight == WEIGHT)
        .values(weight=1.0)
    )
    op.execute(
        _TABLE.update()
        .where(
            _TABLE.c.key == "fxstreet-news",
            _TABLE.c.title_exclude_pattern == _FXSTREET_COMMENTARY,
        )
        .values(title_exclude_pattern=None)
    )
    op.execute(_TABLE.update().where(_TABLE.c.key.in_(REMOVED)).values(deleted_at=None))
    # Soft-deleted sources must stay disabled (ck_newsroom_sources_deleted_disabled).
    op.execute(
        sa.text(
            "UPDATE newsroom_sources SET enabled = true "
            "WHERE key = ANY(:keys) AND deleted_at IS NULL"
        ).bindparams(keys=[*REMOVED, *STOPPED])
    )
