import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PodcastUploadBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "podcast_upload_batches"
    __table_args__ = (
        CheckConstraint("base_episode_version > 0", name="base_episode_version_positive"),
        CheckConstraint("applied_count >= 0", name="applied_count_nonnegative"),
        CheckConstraint(
            "status IN ('open', 'completed', 'conflict', 'expired')", name="status_valid"
        ),
        UniqueConstraint(
            "created_by_user_id", "idempotency_key", name="uq_podcast_upload_batch_actor_key"
        ),
        Index("ix_podcast_upload_batches_expiry", "expires_at", "status"),
    )

    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    episode_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("podcast_episodes.id", ondelete="SET NULL")
    )
    # An absent episode has conceptual version 1, the version used when its
    # first serialized cutover creates the row.
    base_episode_version: Mapped[int] = mapped_column(Integer, nullable=False)
    began_published: Mapped[bool] = mapped_column(Boolean, nullable=False)
    applied_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open", server_default="open"
    )
    request_id: Mapped[str] = mapped_column(String(100), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PodcastUploadSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "podcast_upload_sessions"
    __table_args__ = (
        CheckConstraint("locale IN ('zh-hant', 'zh-hans', 'en')", name="locale_supported"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("size_bytes <= 268435456", name="size_within_limit"),
        CheckConstraint("expected_sha256 ~ '^[0-9a-f]{64}$'", name="expected_sha256_valid"),
        CheckConstraint(
            "expected_current_version IS NULL OR expected_current_version > 0",
            name="expected_version_positive",
        ),
        CheckConstraint(
            "status IN ('pending_upload', 'queued', 'processing', 'completed', "
            "'failed', 'conflict', 'expired')",
            name="status_valid",
        ),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        UniqueConstraint("batch_id", "locale", name="uq_podcast_upload_session_batch_locale"),
        UniqueConstraint("object_key", name="uq_podcast_upload_session_object_key"),
        Index("ix_podcast_upload_sessions_work", "status", "lease_until"),
        Index("ix_podcast_upload_sessions_cleanup", "status", "cleanup_after"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("podcast_upload_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    locale: Mapped[str] = mapped_column(String(10), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_current_version: Mapped[int | None] = mapped_column(Integer)
    upload_url: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending_upload", server_default="pending_upload"
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cleanup_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    chapters: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cleanup_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
