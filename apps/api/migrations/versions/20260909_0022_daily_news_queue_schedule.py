"""Persist automatic daily-news scheduling and targeted retries.

Revision ID: 20260909_0022
Revises: 20260908_0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_0022"
down_revision: str | None = "20260908_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "data_management_runs"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True))
    # Keep the established name so API conflict mapping stays stable, while
    # allowing durable automatic work to coexist with a manual request.
    op.drop_index("uq_data_management_runs_active_news", table_name=TABLE)
    op.create_index(
        "uq_data_management_runs_active_news",
        TABLE,
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'running') AND operation IN ('news_all', 'news_market') "
            "AND requested_by_user_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_data_management_runs_automatic_news_all_edition",
        TABLE,
        ["edition_date"],
        unique=True,
        postgresql_where=sa.text("operation = 'news_all' AND requested_by_user_id IS NULL"),
    )
    op.create_index(
        "uq_data_management_runs_automatic_news_market_retry",
        TABLE,
        ["edition_date", "market_code", "scheduled_for"],
        unique=True,
        postgresql_where=sa.text(
            "operation = 'news_market' AND requested_by_user_id IS NULL "
            "AND scheduled_for IS NOT NULL"
        ),
    )


def downgrade() -> None:
    # A prior binary cannot see scheduled_for or enforce historical automatic
    # edition/retry uniqueness.  Refuse instead of silently turning durable
    # work into immediately claimable rows.
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {TABLE} "
            "WHERE scheduled_for IS NOT NULL "
            "OR (operation = 'news_all' AND requested_by_user_id IS NULL)) THEN "
            "RAISE EXCEPTION "
            "'cannot downgrade while automatic daily-news scheduling history exists'; "
            "END IF; END $$"
        )
    )
    op.drop_index("uq_data_management_runs_automatic_news_market_retry", table_name=TABLE)
    op.drop_index("uq_data_management_runs_automatic_news_all_edition", table_name=TABLE)
    op.drop_index("uq_data_management_runs_active_news", table_name=TABLE)
    op.create_index(
        "uq_data_management_runs_active_news",
        TABLE,
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'running') AND operation IN ('news_all', 'news_market')"
        ),
    )
    op.drop_column(TABLE, "scheduled_for")
