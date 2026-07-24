import uuid

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.enums import GenerationStatus, MessageRole
from daily_insights_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_conversations_org_created", "organization_id", "created_at"),
        Index("ix_conversations_user_created", "user_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str | None] = mapped_column(String(300))


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("sequence_number >= 0", name="sequence_number_nonnegative"),
        UniqueConstraint("conversation_id", "sequence_number"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sequence_number: Mapped[int]
    role: Mapped[MessageRole] = mapped_column(
        Enum(
            MessageRole, name="message_role", values_callable=lambda enum: [e.value for e in enum]
        ),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[GenerationStatus] = mapped_column(
        Enum(
            GenerationStatus,
            name="generation_status",
            values_callable=lambda enum: [e.value for e in enum],
        ),
        nullable=False,
        default=GenerationStatus.COMPLETE,
        server_default=GenerationStatus.COMPLETE.value,
    )
