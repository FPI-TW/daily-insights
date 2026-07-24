from sqlalchemy import CheckConstraint, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(btrim(email))", name="email_normalized"),)

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
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
        default=UserStatus.INVITED,
        server_default=UserStatus.INVITED.value,
        index=True,
    )
