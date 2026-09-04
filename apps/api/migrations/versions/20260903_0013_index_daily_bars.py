"""Store settled daily bars for tracked indices.

Revision ID: 20260903_0013
Revises: 20260903_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260903_0013"
down_revision: str | None = "20260903_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "index_daily_bar_series",
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.PrimaryKeyConstraint("symbol", name="pk_index_daily_bar_series"),
        sa.UniqueConstraint(
            "symbol",
            "provider",
            name="uq_index_daily_bar_series_symbol_provider",
        ),
    )
    op.create_table(
        "index_daily_bars",
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("market_code", sa.String(50), nullable=False),
        sa.Column("open", sa.Numeric(20, 10)),
        sa.Column("high", sa.Numeric(20, 10)),
        sa.Column("low", sa.Numeric(20, 10)),
        sa.Column("close", sa.Numeric(20, 10), nullable=False),
        sa.Column("volume", sa.BigInteger()),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("contract_version", sa.String(100), nullable=False),
        sa.Column("source_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("symbol", "trade_date", name="pk_index_daily_bars"),
        sa.ForeignKeyConstraint(
            ["market_code"],
            ["markets.code"],
            name="fk_index_daily_bars_market_code_markets",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["symbol", "provider"],
            ["index_daily_bar_series.symbol", "index_daily_bar_series.provider"],
            name="fk_index_daily_bars_symbol_index_daily_bar_series",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("close > 0", name="ck_index_daily_bars_close_positive"),
        sa.CheckConstraint(
            "volume IS NULL OR volume >= 0",
            name="ck_index_daily_bars_volume_nonnegative",
        ),
        sa.CheckConstraint(
            "high IS NULL OR low IS NULL OR high >= low",
            name="ck_index_daily_bars_high_not_below_low",
        ),
    )
    op.create_index(
        "ix_index_daily_bars_market_code",
        "index_daily_bars",
        ["market_code"],
    )
    op.create_index(
        "ix_index_daily_bars_market_date",
        "index_daily_bars",
        ["market_code", "trade_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_index_daily_bars_market_date", table_name="index_daily_bars")
    op.drop_index("ix_index_daily_bars_market_code", table_name="index_daily_bars")
    op.drop_table("index_daily_bars")
    op.drop_table("index_daily_bar_series")
