import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.models import Base, UUIDPrimaryKeyMixin


class DataManagementRun(UUIDPrimaryKeyMixin, Base):
    """A restart-safe requested provider operation."""

    __tablename__ = "data_management_runs"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('morning_all', 'morning_market', 'index_yahoo', 'institutional_twse')",
            name="operation_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partial', 'failed')",
            name="status_valid",
        ),
        CheckConstraint(
            "(operation = 'morning_market') = (market_code IS NOT NULL)",
            name="market_scope_matches_operation",
        ),
        Index(
            "uq_data_management_runs_active_morning",
            text("(1)"),
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'running') "
                "AND operation IN ('morning_all', 'morning_market')"
            ),
        ),
        Index(
            "uq_data_management_runs_active_index",
            text("(1)"),
            unique=True,
            postgresql_where=text("status IN ('pending', 'running') AND operation = 'index_yahoo'"),
        ),
        Index(
            "uq_data_management_runs_active_institutional",
            text("(1)"),
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'running') AND operation = 'institutional_twse'"
            ),
        ),
        Index("ix_data_management_runs_created_at", "created_at"),
    )

    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    market_code: Mapped[str | None] = mapped_column(
        String(50), ForeignKey("markets.code", ondelete="RESTRICT")
    )
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )
