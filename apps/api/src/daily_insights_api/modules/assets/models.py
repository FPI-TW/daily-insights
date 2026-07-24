import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.enums import AssetKind, AssetStatus
from daily_insights_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Asset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_bytes_nonnegative"),
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
