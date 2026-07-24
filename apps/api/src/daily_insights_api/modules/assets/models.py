import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.enums import AssetKind, AssetStatus
from daily_insights_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Asset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_bytes_nonnegative"),
        CheckConstraint(
            "status != 'active' OR "
            "(size_bytes > 0 AND sha256 IS NOT NULL AND char_length(sha256) = 64)",
            name="active_metadata_complete",
        ),
        Index("ix_assets_status_kind", "status", "kind"),
    )

    bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    kind: Mapped[AssetKind] = mapped_column(
        Enum(AssetKind, name="asset_kind", values_callable=lambda enum: [e.value for e in enum]),
        nullable=False,
    )
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    locale: Mapped[str | None] = mapped_column(String(10))
    localized_titles: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[AssetStatus] = mapped_column(
        Enum(
            AssetStatus,
            name="asset_status",
            values_callable=lambda enum: [e.value for e in enum],
        ),
        nullable=False,
        default=AssetStatus.ACTIVE,
        server_default=AssetStatus.ACTIVE.value,
    )
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetMigrationManifest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "asset_migration_manifests"
    __table_args__ = (
        CheckConstraint("char_length(idempotency_key) = 64", name="idempotency_key_sha256"),
        CheckConstraint(
            "status IN ('planned', 'copying', 'verified', 'cutover', 'failed')",
            name="status_valid",
        ),
    )

    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="planned", server_default="planned"
    )
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    cutover_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    cutover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)


class AssetMigrationEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "asset_migration_entries"
    __table_args__ = (
        CheckConstraint("locale IN ('zh-TW', 'zh-CN', 'en')", name="locale_supported"),
        CheckConstraint(
            "source_size_bytes IS NULL OR source_size_bytes > 0", name="source_size_positive"
        ),
        CheckConstraint(
            "target_size_bytes IS NULL OR target_size_bytes > 0", name="target_size_positive"
        ),
        CheckConstraint(
            "source_sha256 IS NULL OR char_length(source_sha256) = 64",
            name="source_sha256_valid",
        ),
        CheckConstraint(
            "target_sha256 IS NULL OR char_length(target_sha256) = 64",
            name="target_sha256_valid",
        ),
        CheckConstraint(
            "status IN ('planned', 'copied', 'verified', 'cutover', 'failed')",
            name="status_valid",
        ),
        UniqueConstraint("manifest_id", "source_bucket", "source_key"),
        UniqueConstraint("target_bucket", "target_key"),
        Index("ix_asset_migration_entries_manifest_status", "manifest_id", "status"),
    )

    manifest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("asset_migration_manifests.id", ondelete="CASCADE"),
        nullable=False,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    locale: Mapped[str] = mapped_column(String(10), nullable=False)
    source_bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    source_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    target_bucket: Mapped[str] = mapped_column(String(100), nullable=False)
    target_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    target_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    source_mime_type: Mapped[str | None] = mapped_column(String(255))
    target_mime_type: Mapped[str | None] = mapped_column(String(255))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    target_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="planned", server_default="planned"
    )
    copied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cutover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
