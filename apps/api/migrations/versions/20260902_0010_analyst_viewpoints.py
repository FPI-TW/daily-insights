"""Persist daily analyst viewpoints.

Revision ID: 20260902_0010
Revises: 20260902_0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260902_0010"
down_revision: str | None = "20260902_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analyst_viewpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("viewpoint_date", sa.Date(), nullable=False),
        sa.Column("market_code", sa.String(length=50), nullable=False),
        sa.Column("source_market_code", sa.String(length=50), nullable=False),
        sa.Column("points", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("jsonb_typeof(points) = 'array'", name="points_are_array"),
        sa.ForeignKeyConstraint(["market_code"], ["markets.code"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "viewpoint_date", "market_code", name="uq_analyst_viewpoint_date_market"
        ),
    )
    op.create_index(
        "ix_analyst_viewpoints_date_market",
        "analyst_viewpoints",
        ["viewpoint_date", "market_code"],
    )


def downgrade() -> None:
    op.drop_index("ix_analyst_viewpoints_date_market", table_name="analyst_viewpoints")
    op.drop_table("analyst_viewpoints")
