import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Market(Base):
    __tablename__ = "markets"

    code: Mapped[str] = mapped_column(String(50), primary_key=True)
    name_en: Mapped[str] = mapped_column(String(100), nullable=False)
    name_zh_hant: Mapped[str] = mapped_column(String(100), nullable=False)
    name_zh_hans: Mapped[str] = mapped_column(String(100), nullable=False)


class OrganizationMarketPolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An audited override; no row means the market is visible."""

    __tablename__ = "organization_market_policies"
    __table_args__ = (UniqueConstraint("organization_id", "market_code"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    market_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("markets.code", ondelete="RESTRICT"), nullable=False, index=True
    )
    is_visible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    contract_reference: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(String(500))
    changed_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IndexDailyBarSeries(Base):
    """Own one symbol's complete history for exactly one provider."""

    __tablename__ = "index_daily_bar_series"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "provider",
            name="uq_index_daily_bar_series_symbol_provider",
        ),
    )

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)


class IndexDailyBar(TimestampMixin, Base):
    """One settled trading day for one tracked index.

    Keyed on (symbol, trade_date) rather than a surrogate id: a daily bar is an
    immutable fact, so re-fetching a window must confirm the existing rows, not
    grow a second copy of them. Provenance lives on the row because the fetch is
    deterministic and has nothing an audit table could add.
    """

    __tablename__ = "index_daily_bars"
    __table_args__ = (
        CheckConstraint("close > 0", name="close_positive"),
        CheckConstraint("volume IS NULL OR volume >= 0", name="volume_nonnegative"),
        CheckConstraint(
            "high IS NULL OR low IS NULL OR high >= low",
            name="high_not_below_low",
        ),
        ForeignKeyConstraint(
            ["symbol", "provider"],
            ["index_daily_bar_series.symbol", "index_daily_bar_series.provider"],
            ondelete="RESTRICT",
        ),
        Index("ix_index_daily_bars_market_date", "market_code", "trade_date"),
    )

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    market_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("markets.code", ondelete="RESTRICT"), nullable=False, index=True
    )
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    close: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
