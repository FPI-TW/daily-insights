"""Record analyst viewpoint sync execution outcomes.

Revision ID: 20260902_0011
Revises: 20260902_0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260902_0011"
down_revision: str | None = "20260902_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analyst_viewpoint_sync_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("viewpoint_date", sa.Date(), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("markets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("trigger IN ('scheduler', 'manual')", name="trigger_valid"),
        sa.CheckConstraint("status IN ('complete', 'partial', 'failed')", name="status_valid"),
        sa.CheckConstraint("jsonb_typeof(markets) = 'array'", name="markets_are_array"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_analyst_viewpoint_sync_runs_completed",
        "analyst_viewpoint_sync_runs",
        ["completed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analyst_viewpoint_sync_runs_completed",
        table_name="analyst_viewpoint_sync_runs",
    )
    op.drop_table("analyst_viewpoint_sync_runs")
