"""Store TWSE trade value on index daily bars.

Revision ID: 20260915_0026
Revises: 20260911_0024
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0026"
down_revision: str | None = "20260911_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable TWD trade value; the TWSE scheduler backfills it."""
    op.add_column("index_daily_bars", sa.Column("trade_value", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "ck_index_daily_bars_trade_value_nonnegative",
        "index_daily_bars",
        "trade_value IS NULL OR trade_value >= 0",
    )


def downgrade() -> None:
    """Remove only trade value while retaining every existing daily bar."""
    op.drop_constraint(
        "ck_index_daily_bars_trade_value_nonnegative", "index_daily_bars", type_="check"
    )
    op.drop_column("index_daily_bars", "trade_value")
