"""Add durable browser direct-upload batches and processing sessions.

Revision ID: 20260924_0030
Revises: 20260918_0029
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260924_0030"
down_revision: str | None = "20260918_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "podcast_upload_batches",
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=30), nullable=False),
        sa.Column("episode_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("base_episode_version", sa.Integer(), nullable=False),
        sa.Column("began_published", sa.Boolean(), nullable=False),
        sa.Column("applied_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="open", nullable=False),
        sa.Column("request_id", sa.String(length=100), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "base_episode_version > 0",
            name="ck_podcast_upload_batches_base_episode_version_positive",
        ),
        sa.CheckConstraint(
            "applied_count >= 0", name="ck_podcast_upload_batches_applied_count_nonnegative"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'completed', 'conflict', 'expired')",
            name="ck_podcast_upload_batches_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_podcast_upload_batches_created_by_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["podcast_episodes.id"],
            ondelete="SET NULL",
            name="fk_podcast_upload_batches_episode_id_podcast_episodes",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_podcast_upload_batches"),
        sa.UniqueConstraint(
            "created_by_user_id", "idempotency_key", name="uq_podcast_upload_batch_actor_key"
        ),
    )
    op.create_index(
        "ix_podcast_upload_batches_expiry",
        "podcast_upload_batches",
        ["expires_at", "status"],
        unique=False,
    )

    op.create_table(
        "podcast_upload_sessions",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_current_version", sa.Integer(), nullable=True),
        sa.Column("upload_url", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending_upload", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleanup_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "chapters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cleanup_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "locale IN ('zh-hant', 'zh-hans', 'en')",
            name="ck_podcast_upload_sessions_locale_supported",
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_podcast_upload_sessions_size_positive"),
        sa.CheckConstraint(
            "expected_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_podcast_upload_sessions_expected_sha256_valid",
        ),
        sa.CheckConstraint(
            "size_bytes <= 268435456", name="ck_podcast_upload_sessions_size_within_limit"
        ),
        sa.CheckConstraint(
            "expected_current_version IS NULL OR expected_current_version > 0",
            name="ck_podcast_upload_sessions_expected_version_positive",
        ),
        sa.CheckConstraint(
            "status IN ('pending_upload', 'queued', 'processing', 'completed', "
            "'failed', 'conflict', 'expired')",
            name="ck_podcast_upload_sessions_status_valid",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_podcast_upload_sessions_attempts_nonnegative"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["podcast_upload_batches.id"],
            ondelete="CASCADE",
            name="fk_podcast_upload_sessions_batch_id_podcast_upload_batches",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_podcast_upload_sessions"),
        sa.UniqueConstraint("asset_id", name="uq_podcast_upload_sessions_asset_id"),
        sa.UniqueConstraint("batch_id", "locale", name="uq_podcast_upload_session_batch_locale"),
        sa.UniqueConstraint("object_key", name="uq_podcast_upload_session_object_key"),
    )
    op.create_index(
        "ix_podcast_upload_sessions_work",
        "podcast_upload_sessions",
        ["status", "lease_until"],
        unique=False,
    )
    op.create_index(
        "ix_podcast_upload_sessions_cleanup",
        "podcast_upload_sessions",
        ["status", "cleanup_after"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_podcast_upload_sessions_cleanup", table_name="podcast_upload_sessions")
    op.drop_index("ix_podcast_upload_sessions_work", table_name="podcast_upload_sessions")
    op.drop_table("podcast_upload_sessions")
    op.drop_index("ix_podcast_upload_batches_expiry", table_name="podcast_upload_batches")
    op.drop_table("podcast_upload_batches")
