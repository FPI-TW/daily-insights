"""Durable episode removal and upload generation fencing.

Revision ID: 20261007_0032
Revises: 20261005_0031
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261007_0032"
down_revision: str | None = "20261005_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.add_column(
        "asset_migration_entries",
        sa.Column("podcast_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "podcast_episodes",
        sa.Column("deletion_pending", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "podcast_upload_batches",
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "podcast_date_generations",
        sa.Column("trading_date", sa.Date(), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("generation >= 0", name="generation_nonnegative"),
    )
    op.create_table(
        "podcast_deletion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("episode_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("episode_version", sa.Integer(), nullable=False),
        sa.Column("requested_version", sa.Integer(), nullable=False),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending', 'completed')", name="status_valid"),
        *timestamps(),
    )
    op.create_table(
        "podcast_deletion_objects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("podcast_deletion_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bucket", sa.String(100), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.CheckConstraint("status IN ('pending', 'deleted', 'shared')", name="status_valid"),
        sa.UniqueConstraint("job_id", "asset_id", name="uq_podcast_deletion_object_asset"),
        *timestamps(),
    )


def downgrade() -> None:
    op.drop_column("asset_migration_entries", "podcast_generation")
    op.drop_table("podcast_deletion_objects")
    op.drop_table("podcast_deletion_jobs")
    op.drop_table("podcast_date_generations")
    op.drop_column("podcast_upload_batches", "generation")
    op.drop_column("podcast_episodes", "deletion_pending")
