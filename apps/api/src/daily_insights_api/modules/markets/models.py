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


# TWSE investor categories shared by the market-level (BFI82U) and per-stock (T86)
# reports. The five are disjoint, and the totals TWSE also publishes ("合計",
# "自營商", "三大法人") are their sums, so none of those is stored:
#   自營商   = dealer_self + dealer_hedge      (holds on all 1,340 T86 rows)
#   三大法人 = foreign + foreign_dealer + trust + 自營商
# foreign_dealer is a slice of the foreign side, not of the dealer books: BFI82U
# labels its sibling row 外資及陸資(不含外資自營商) precisely because the two add back
# up to 外資及陸資, which is the single row the report carries when it is not asked
# to split it (自營商 自行/避險, 投信, 外資及陸資, 合計).
INVESTOR_TYPES: tuple[str, ...] = (
    "foreign",  # 外資及陸資(不含外資自營商)
    "foreign_dealer",  # 外資自營商
    "trust",  # 投信
    "dealer_self",  # 自營商(自行買賣)
    "dealer_hedge",  # 自營商(避險)
)
_INVESTOR_TYPE_SQL = ", ".join(f"'{code}'" for code in INVESTOR_TYPES)


class InstitutionalMarketFlow(TimestampMixin, Base):
    """One investor category's whole-market buy/sell amount (TWD) for one trading day."""

    __tablename__ = "institutional_market_flows"
    __table_args__ = (
        CheckConstraint(f"investor_type IN ({_INVESTOR_TYPE_SQL})", name="investor_type"),
        CheckConstraint("buy_amount >= 0 AND sell_amount >= 0", name="amounts_nonnegative"),
        CheckConstraint("net_amount = buy_amount - sell_amount", name="net_is_buy_minus_sell"),
        Index("ix_institutional_market_flows_market_date", "market_code", "trade_date"),
    )

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    investor_type: Mapped[str] = mapped_column(String(30), primary_key=True)
    market_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("markets.code", ondelete="RESTRICT"), nullable=False
    )
    buy_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sell_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InstitutionalStockFlow(TimestampMixin, Base):
    """One investor category's buy/sell share count for one security on one trading day."""

    __tablename__ = "institutional_stock_flows"
    __table_args__ = (
        CheckConstraint(f"investor_type IN ({_INVESTOR_TYPE_SQL})", name="investor_type"),
        CheckConstraint("buy_shares >= 0 AND sell_shares >= 0", name="shares_nonnegative"),
        CheckConstraint("net_shares = buy_shares - sell_shares", name="net_is_buy_minus_sell"),
        Index(
            "ix_institutional_stock_flows_date_investor_net",
            "trade_date",
            "investor_type",
            "net_shares",
        ),
        Index("ix_institutional_stock_flows_symbol_date", "symbol", "trade_date"),
    )

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    investor_type: Mapped[str] = mapped_column(String(30), primary_key=True)
    market_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("markets.code", ondelete="RESTRICT"), nullable=False, index=True
    )
    # ponytail: no security master table yet, so the name rides along on every row.
    security_name: Mapped[str] = mapped_column(String(100), nullable=False)
    buy_shares: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sell_shares: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_shares: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
