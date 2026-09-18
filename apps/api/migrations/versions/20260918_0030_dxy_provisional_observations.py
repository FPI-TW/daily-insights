"""Track provisional market observations.

Revision ID: 20260918_0030
Revises: 20260918_0029
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260918_0030"
down_revision: str | None = "20260918_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "market_daily_observations"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("is_provisional", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column(TABLE, "is_provisional", server_default=None)


def downgrade() -> None:
    op.drop_column(TABLE, "is_provisional")
