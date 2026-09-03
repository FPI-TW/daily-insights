from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
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
