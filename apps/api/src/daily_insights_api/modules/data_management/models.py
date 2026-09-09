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
            (
                "operation IN ('morning_all', 'morning_market', 'index_yahoo', "
                "'institutional_twse', 'news_all', 'news_market', 'macro_dashboard')"
            ),
            name="operation_valid",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'cancelled')",
            name="status_valid",
        ),
        CheckConstraint(
            "("
            "(operation = 'morning_market' AND market_code IN "
            "('global_macro_bonds', 'crypto', 'us_equity')) OR "
            "(operation = 'news_market' AND market_code IN "
            "('global', 'tw_equity', 'us_equity')) OR "
            "(operation IN ('morning_all', 'index_yahoo', 'institutional_twse', 'news_all', "
            "'macro_dashboard') "
            "AND market_code IS NULL)"
            ")",
            name="market_code_valid_for_operation",
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
        Index(
            "uq_data_management_runs_active_news",
            text("(1)"),
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'running') AND operation IN ('news_all', 'news_market') "
                "AND requested_by_user_id IS NOT NULL"
            ),
        ),
        # The 08:00 obligation is durable history, rather than an in-memory
        # scheduler marker.  Retrying a terminal automatic row must therefore
        # never create another automatic all-market run for that edition.
        Index(
            "uq_data_management_runs_automatic_news_all_edition",
            "edition_date",
            unique=True,
            postgresql_where=text("operation = 'news_all' AND requested_by_user_id IS NULL"),
        ),
        # A retry is an independently durable, future-due market operation.
        # Keeping historical rows in the key makes a worker crash/recovery
        # unable to enqueue the same scheduled retry twice.
        Index(
            "uq_data_management_runs_automatic_news_market_retry",
            "edition_date",
            "market_code",
            "scheduled_for",
            unique=True,
            postgresql_where=text(
                "operation = 'news_market' AND requested_by_user_id IS NULL "
                "AND scheduled_for IS NOT NULL"
            ),
        ),
        Index(
            "uq_data_management_runs_active_manual_macro_dashboard",
            text("(1)"),
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'running') AND operation = 'macro_dashboard' "
                "AND requested_by_user_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_data_management_runs_running_macro_dashboard",
            text("(1)"),
            unique=True,
            postgresql_where=text("status = 'running' AND operation = 'macro_dashboard'"),
        ),
        # Scheduled macro work is an edition record, not a retry mechanism.
        # Manual rows retain their history and are deliberately not covered.
        Index(
            "uq_data_management_runs_automatic_macro_dashboard_edition",
            "edition_date",
            unique=True,
            postgresql_where=text("operation = 'macro_dashboard' AND requested_by_user_id IS NULL"),
        ),
        Index("ix_data_management_runs_created_at", "created_at"),
    )

    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    # This is an operation scope, not always a row in the market catalog:
    # news has a valid cross-market ``global`` edition.  The operation-specific
    # check constraint above keeps each operation's scope finite and valid.
    market_code: Mapped[str | None] = mapped_column(String(50))
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    # Null when the scheduler queued it: nobody asked, it was due.
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Manual work and the initial automatic all-market edition are eligible as
    # soon as they are queued.  Automatic market retry rows are not claimable
    # until this durable Taipei-time timestamp.
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
