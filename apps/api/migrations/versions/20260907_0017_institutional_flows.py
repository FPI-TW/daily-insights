"""Store TWSE institutional investor flows: market totals (amount) and per-stock (shares).

Revision ID: 20260907_0017
Revises: 20260907_0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260907_0017"
down_revision: str | None = "20260907_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INVESTOR_TYPES = ("foreign", "foreign_dealer", "trust", "dealer_self", "dealer_hedge")
_investor_type_sql = ", ".join(f"'{code}'" for code in INVESTOR_TYPES)


def upgrade() -> None:
    op.create_table(
        "institutional_market_flows",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("investor_type", sa.String(30), nullable=False),
        sa.Column("market_code", sa.String(50), nullable=False),
        sa.Column("buy_amount", sa.BigInteger(), nullable=False),
        sa.Column("sell_amount", sa.BigInteger(), nullable=False),
        sa.Column("net_amount", sa.BigInteger(), nullable=False),
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
        sa.PrimaryKeyConstraint(
            "trade_date", "investor_type", name="pk_institutional_market_flows"
        ),
        sa.ForeignKeyConstraint(
            ["market_code"],
            ["markets.code"],
            name="fk_institutional_market_flows_market_code_markets",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"investor_type IN ({_investor_type_sql})",
            name="investor_type",
        ),
        sa.CheckConstraint(
            "buy_amount >= 0 AND sell_amount >= 0",
            name="amounts_nonnegative",
        ),
        sa.CheckConstraint(
            "net_amount = buy_amount - sell_amount",
            name="net_is_buy_minus_sell",
        ),
    )
    op.create_index(
        "ix_institutional_market_flows_market_date",
        "institutional_market_flows",
        ["market_code", "trade_date"],
    )

    op.create_table(
        "institutional_stock_flows",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("investor_type", sa.String(30), nullable=False),
        sa.Column("market_code", sa.String(50), nullable=False),
        sa.Column("security_name", sa.String(100), nullable=False),
        sa.Column("buy_shares", sa.BigInteger(), nullable=False),
        sa.Column("sell_shares", sa.BigInteger(), nullable=False),
        sa.Column("net_shares", sa.BigInteger(), nullable=False),
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
        sa.PrimaryKeyConstraint(
            "trade_date", "symbol", "investor_type", name="pk_institutional_stock_flows"
        ),
        sa.ForeignKeyConstraint(
            ["market_code"],
            ["markets.code"],
            name="fk_institutional_stock_flows_market_code_markets",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"investor_type IN ({_investor_type_sql})",
            name="investor_type",
        ),
        sa.CheckConstraint(
            "buy_shares >= 0 AND sell_shares >= 0",
            name="shares_nonnegative",
        ),
        sa.CheckConstraint(
            "net_shares = buy_shares - sell_shares",
            name="net_is_buy_minus_sell",
        ),
    )
    # Ranking query: "top N net buys by investor X on day D".
    op.create_index(
        "ix_institutional_stock_flows_date_investor_net",
        "institutional_stock_flows",
        ["trade_date", "investor_type", "net_shares"],
    )
    # Per-stock history query: one symbol across days.
    op.create_index(
        "ix_institutional_stock_flows_symbol_date",
        "institutional_stock_flows",
        ["symbol", "trade_date"],
    )
    op.create_index(
        "ix_institutional_stock_flows_market_code",
        "institutional_stock_flows",
        ["market_code"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_institutional_stock_flows_market_code", table_name="institutional_stock_flows"
    )
    op.drop_index(
        "ix_institutional_stock_flows_symbol_date", table_name="institutional_stock_flows"
    )
    op.drop_index(
        "ix_institutional_stock_flows_date_investor_net", table_name="institutional_stock_flows"
    )
    op.drop_table("institutional_stock_flows")
    op.drop_index(
        "ix_institutional_market_flows_market_date", table_name="institutional_market_flows"
    )
    op.drop_table("institutional_market_flows")
