import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.enums import GenerationStatus
from daily_insights_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ModelConfiguration(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "model_configurations"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        Index(
            "uq_model_configurations_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        UniqueConstraint("id", "version", name="uq_model_configurations_id_version"),
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    parameters: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class GenerationRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "generation_records"
    __table_args__ = (
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0", name="input_tokens_nonnegative"
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0", name="output_tokens_nonnegative"
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_nonnegative"),
        ForeignKeyConstraint(
            ["model_configuration_id", "model_configuration_version"],
            ["model_configurations.id", "model_configurations.version"],
            ondelete="RESTRICT",
        ),
        Index("ix_generation_records_status_created", "status", "created_at"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    model_configuration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    model_configuration_version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(200), nullable=False)
    resolved_model: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    context_version: Mapped[str | None] = mapped_column(String(100))
    report_version: Mapped[str | None] = mapped_column(String(100))
    parameters: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), index=True)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    latency_ms: Mapped[int | None]
    status: Mapped[GenerationStatus] = mapped_column(
        Enum(
            GenerationStatus,
            name="generation_status",
            values_callable=lambda enum: [e.value for e in enum],
            create_type=False,
        ),
        nullable=False,
        default=GenerationStatus.PENDING,
        server_default=GenerationStatus.PENDING.value,
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(Text)
