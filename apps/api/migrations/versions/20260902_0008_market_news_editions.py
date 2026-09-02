"""Add per-market daily news editions.

Revision ID: 20260902_0008
Revises: 20260901_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0008"
down_revision: str | None = "20260901_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MARKET_CODES = ("global", "tw_equity", "us_equity")


def upgrade() -> None:
    op.add_column(
        "news_editions",
        sa.Column("market_code", sa.String(50), nullable=False, server_default="global"),
    )
    op.drop_constraint("uq_news_edition_version", "news_editions", type_="unique")
    op.create_unique_constraint(
        "uq_news_edition_version",
        "news_editions",
        ["edition_date", "market_code", "revision"],
    )
    op.drop_index("ix_news_editions_latest", table_name="news_editions")
    op.create_index(
        "ix_news_editions_latest",
        "news_editions",
        ["edition_date", "market_code", "revision"],
    )
    op.create_check_constraint(
        "market_code_valid",
        "news_editions",
        "market_code IN ('global','tw_equity','us_equity')",
    )


def downgrade() -> None:
    connection = op.get_bind()
    non_global = connection.execute(
        sa.text("SELECT count(*) FROM news_editions WHERE market_code <> 'global'")
    ).scalar_one()
    if non_global:
        raise RuntimeError(
            "news_editions contains per-market editions; remove them before downgrading"
        )
    op.drop_constraint("market_code_valid", "news_editions", type_="check")
    op.drop_index("ix_news_editions_latest", table_name="news_editions")
    op.create_index("ix_news_editions_latest", "news_editions", ["edition_date", "revision"])
    op.drop_constraint("uq_news_edition_version", "news_editions", type_="unique")
    op.create_unique_constraint(
        "uq_news_edition_version", "news_editions", ["edition_date", "revision"]
    )
    op.drop_column("news_editions", "market_code")
