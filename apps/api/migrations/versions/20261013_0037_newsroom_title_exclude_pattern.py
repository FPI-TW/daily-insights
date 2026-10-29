"""Let a source drop feed entries by title, and drop cnyes' machine-written flashes.

Revision ID: 20261013_0037
Revises: 20261013_0036

``newsroom_sources.title_exclude_pattern`` is a regular expression searched in
each feed entry's title; matching entries are never stored, so they cost no
fetch, embedding or triage call.

The three cnyes feeds get a pattern for their machine-written items (byline
「鉅亨網新聞中心」): intraday price flashes (盤中速報、盤後速報、焦點速報、外匯速報、
鉅亨買幣速報), FactSet target-price screens (鉅亨速報), revenue tables (營收速報),
institutional buy/sell lists (外資、投信、自營商近 N 日買賣超個股) and the
pre-market screens (【鉅亨…盤前掃瞄】). Over 2026-10-04 to 10-13 it matched
1,607 of the 1,668 machine-written cnyes articles and none written by reporters,
and every edition event that contained a match had other reports.

The downgrade drops the column, and with it any pattern set since.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261013_0037"
down_revision: str | None = "20261013_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CNYES_KEYS = ("cnyes-headline", "cnyes-tw-stock", "cnyes-wd-stock")
CNYES_MACHINE_TITLES = (
    r"^\s*(?:盤中速報|盤後速報|焦點速報|外匯速報|鉅亨速報|鉅亨買幣速報|營收速報)"
    r"|^\s*(?:外資|投信|自營商)近\s*\d+\s*日[買賣]超個股"
    r"|^\s*【鉅亨.{0,10}盤前掃瞄】"
)


def upgrade() -> None:
    op.add_column(
        "newsroom_sources", sa.Column("title_exclude_pattern", sa.String(length=500), nullable=True)
    )
    op.execute(
        sa.text(
            "UPDATE newsroom_sources SET title_exclude_pattern = :pattern "
            "WHERE key = ANY(:keys) AND title_exclude_pattern IS NULL"
        ).bindparams(pattern=CNYES_MACHINE_TITLES, keys=list(CNYES_KEYS))
    )


def downgrade() -> None:
    op.drop_column("newsroom_sources", "title_exclude_pattern")
