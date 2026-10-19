"""Allow ``html_list`` and seed MoneyDJ listing source.

Revision ID: 20261015_0043
Revises: 20261014_0043

HTML listing pages are polled like feeds and parsed for article anchors via
``options.href_contains``. robots.txt remains governed by
``DAILY_INSIGHTS_NEWS_IGNORE_ROBOTS``. MoneyDJ has no stable RSS, so this
migration inserts ``moneydj-mb00`` when that key is absent.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261015_0043"
down_revision: str | None = "20261014_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMESPACE = uuid.UUID("5b0c6d8e-2f4a-4b8e-9a51-6e1f0d3c7a20")
_MONEYDJ_KEY = "moneydj-mb00"

_KINDS = (
    "rss",
    "rdf",
    "atom",
    "rss_full",
    "news_sitemap",
    "json_list",
    "html_list",
    "guardian_api",
    "manual",
)
_OLD_KINDS = (
    "rss",
    "rdf",
    "atom",
    "rss_full",
    "news_sitemap",
    "json_list",
    "guardian_api",
    "manual",
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
    sa.column("markets", postgresql.ARRAY(sa.String())),
    sa.column("full_text_in_feed", sa.Boolean()),
    sa.column("options", postgresql.JSONB()),
    sa.column("trust_tier", sa.Integer()),
    sa.column("weight", sa.Float()),
    sa.column("poll_interval_minutes", sa.Integer()),
    sa.column("enabled", sa.Boolean()),
)


def _kind_sql(kinds: tuple[str, ...]) -> str:
    return "(" + ",".join(f"'{kind}'" for kind in kinds) + ")"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_newsroom_sources_kind_valid"), "newsroom_sources", type_="check")
    op.create_check_constraint(
        op.f("ck_newsroom_sources_kind_valid"),
        "newsroom_sources",
        f"kind IN {_kind_sql(_KINDS)}",
    )
    connection = op.get_bind()
    exists = connection.execute(
        sa.text("SELECT 1 FROM newsroom_sources WHERE key = :key"),
        {"key": _MONEYDJ_KEY},
    ).scalar()
    if exists is None:
        op.bulk_insert(
            _TABLE,
            [
                {
                    "id": uuid.uuid5(_NAMESPACE, _MONEYDJ_KEY),
                    "key": _MONEYDJ_KEY,
                    "name": "MoneyDJ",
                    "kind": "html_list",
                    "url": "https://www.moneydj.com/KMDJ/News/NewsRealList.aspx?a=MB00",
                    "hostname": "www.moneydj.com",
                    "link_pattern": (
                        r"(?i)^https://www\.moneydj\.com/kmdj/news/newsviewer\.aspx\?a=[0-9a-f-]+"
                    ),
                    "markets": ["tw_equity"],
                    "full_text_in_feed": False,
                    "options": {
                        "href_contains": "NewsViewer.aspx",
                        "keep_query": True,
                    },
                    "trust_tier": 2,
                    "weight": 1.0,
                    "poll_interval_minutes": 15,
                    "enabled": True,
                }
            ],
        )


def downgrade() -> None:
    # Soft-disable every html_list row (including the MoneyDJ seed) before the
    # kind check shrinks; articles keep their FK to the source row.
    op.execute(
        "UPDATE newsroom_sources SET enabled = false, "
        "deleted_at = COALESCE(deleted_at, now()), kind = 'rss', "
        "options = '{}'::jsonb WHERE kind = 'html_list'"
    )
    op.drop_constraint(op.f("ck_newsroom_sources_kind_valid"), "newsroom_sources", type_="check")
    op.create_check_constraint(
        op.f("ck_newsroom_sources_kind_valid"),
        "newsroom_sources",
        f"kind IN {_kind_sql(_OLD_KINDS)}",
    )
