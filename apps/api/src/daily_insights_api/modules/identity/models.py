from sqlalchemy import Boolean, CheckConstraint, Enum, String, true
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(btrim(email))", name="email_normalized"),)

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    system_role: Mapped[SystemRole] = mapped_column(
        Enum(SystemRole, name="system_role", values_callable=lambda enum: [e.value for e in enum]),
        nullable=False,
        default=SystemRole.ORG_MEMBER,
        server_default=SystemRole.ORG_MEMBER.value,
        index=True,
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status", values_callable=lambda enum: [e.value for e in enum]),
        nullable=False,
        default=UserStatus.ACTIVE,
        server_default=UserStatus.ACTIVE.value,
        index=True,
    )
