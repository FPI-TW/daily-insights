"""Ledger for captured sector heatmap PNGs stored in R2.

Revision ID: 20261012_0033
Revises: 20261007_0032
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261012_0033"
down_revision: str | None = "20261007_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sector_heatmap_images",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("data_source", sa.String(20), nullable=False),
        sa.Column("market_code", sa.String(50), nullable=False),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(200), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("function_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sector_heatmap_images"),
        sa.ForeignKeyConstraint(
            ["market_code"],
            ["markets.code"],
            name="fk_sector_heatmap_images_market_code_markets",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["function_attempt_id"],
            ["function_attempts.id"],
            name="fk_sector_heatmap_images_function_attempt_id_function_attempts",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "data_source", "edition_date", "slug", name="uq_sector_heatmap_image_date_slug"
        ),
        sa.UniqueConstraint("object_key", name="uq_sector_heatmap_image_object_key"),
        sa.CheckConstraint("kind IN ('overview', 'legend')", name="kind_valid"),
        sa.CheckConstraint("sort_order >= 0", name="sort_order_nonnegative"),
        sa.CheckConstraint("char_length(sha256) = 64", name="sha256_length"),
        sa.CheckConstraint(
            "size_bytes > 0 AND width > 0 AND height > 0", name="image_dimensions_positive"
        ),
    )
    op.create_index(
        "ix_sector_heatmap_images_market_date",
        "sector_heatmap_images",
        ["market_code", "edition_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_sector_heatmap_images_market_date", table_name="sector_heatmap_images")
    op.drop_table("sector_heatmap_images")
