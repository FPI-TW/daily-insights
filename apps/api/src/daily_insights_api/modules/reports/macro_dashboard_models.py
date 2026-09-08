"""Durable singleton snapshot for the global macro dashboard."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.models import Base


class MacroDashboardSnapshot(Base):
    """The latest validated dashboard payload for one independently refreshable scope."""

    __tablename__ = "macro_dashboard_snapshots"

    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )
