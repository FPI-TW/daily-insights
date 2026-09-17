import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.models import Base, UUIDPrimaryKeyMixin


class AnalystViewpoint(UUIDPrimaryKeyMixin, Base):
    """The latest valid upstream viewpoint for one Taipei date and market."""

    __tablename__ = "analyst_viewpoints"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(points) = 'array'", name="points_are_array"),
        UniqueConstraint("viewpoint_date", "market_code", name="uq_analyst_viewpoint_date_market"),
        Index("ix_analyst_viewpoints_date_market", "viewpoint_date", "market_code"),
    )

    viewpoint_date: Mapped[date] = mapped_column(Date, nullable=False)
    market_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("markets.code", ondelete="RESTRICT"), nullable=False
    )
    source_market_code: Mapped[str] = mapped_column(String(50), nullable=False)
    points: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analyst_viewpoint_versions.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AnalystViewpointSyncRun(UUIDPrimaryKeyMixin, Base):
    """A durable execution outcome for both unattended and manual syncs."""

    __tablename__ = "analyst_viewpoint_sync_runs"
    __table_args__ = (
        CheckConstraint("trigger IN ('scheduler', 'manual')", name="trigger_valid"),
        CheckConstraint("status IN ('complete', 'partial', 'failed')", name="status_valid"),
        CheckConstraint("jsonb_typeof(markets) = 'array'", name="markets_are_array"),
        Index("ix_analyst_viewpoint_sync_runs_completed", "completed_at"),
    )

    viewpoint_date: Mapped[date] = mapped_column(Date, nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    markets: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    function_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("function_attempts.id", ondelete="RESTRICT")
    )


class AnalystViewpointVersion(UUIDPrimaryKeyMixin, Base):
    """Append-only analyst content owned by one orchestration attempt."""

    __tablename__ = "analyst_viewpoint_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("jsonb_typeof(points) = 'array'", name="points_are_array"),
        CheckConstraint("char_length(content_digest) = 64", name="content_digest_sha256"),
        UniqueConstraint(
            "viewpoint_date",
            "market_code",
            "version",
            name="uq_analyst_viewpoint_version",
        ),
        Index("ix_analyst_viewpoint_versions_latest", "viewpoint_date", "market_code", "version"),
    )

    viewpoint_date: Mapped[date] = mapped_column(Date, nullable=False)
    market_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("markets.code", ondelete="RESTRICT"), nullable=False
    )
    source_market_code: Mapped[str] = mapped_column(String(50), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    points: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    function_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("function_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
