import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, true
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
