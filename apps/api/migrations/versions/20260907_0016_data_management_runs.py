"""Add durable administrator data-management operations.

Revision ID: 20260907_0016
Revises: 20260905_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260907_0016"
down_revision: str | None = "20260905_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_management_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("market_code", sa.String(50)),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("error", sa.String(500)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_data_management_runs"),
        sa.ForeignKeyConstraint(["market_code"], ["markets.code"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "operation IN ('morning_all', 'morning_market', 'index_yahoo')",
            name="ck_data_management_runs_operation_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partial', 'failed')",
            name="ck_data_management_runs_status_valid",
        ),
        sa.CheckConstraint(
            "(operation = 'morning_market') = (market_code IS NOT NULL)",
            name="ck_data_management_runs_market_scope_matches_operation",
        ),
    )
    op.create_index("ix_data_management_runs_created_at", "data_management_runs", ["created_at"])
    op.create_index(
        "uq_data_management_runs_active_morning",
        "data_management_runs",
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'running') AND operation IN ('morning_all', 'morning_market')"
        ),
    )
    op.create_index(
        "uq_data_management_runs_active_index",
        "data_management_runs",
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running') AND operation = 'index_yahoo'"),
    )


def downgrade() -> None:
    op.drop_index("uq_data_management_runs_active_index", table_name="data_management_runs")
    op.drop_index("uq_data_management_runs_active_morning", table_name="data_management_runs")
    op.drop_index("ix_data_management_runs_created_at", table_name="data_management_runs")
    op.drop_table("data_management_runs")
