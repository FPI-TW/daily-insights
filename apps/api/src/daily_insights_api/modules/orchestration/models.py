import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.models import Base, UUIDPrimaryKeyMixin

FUNCTION_STATUSES = (
    "pending",
    "running",
    "retry_wait",
    "succeeded",
    "no_change",
    "partial",
    "unavailable",
    "failed",
    "cancelled",
)
RUN_STATUSES = ("pending", "running", "succeeded", "partial", "failed", "cancelled")


class RoutineRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "routine_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','succeeded','partial','failed','cancelled')",
            name="status_valid",
        ),
        UniqueConstraint(
            "routine_key",
            "edition_date",
            name="uq_routine_run_edition",
        ),
        Index("ix_routine_runs_created_at", "created_at"),
    )

    routine_key: Mapped[str] = mapped_column(String(100), nullable=False)
    registry_version: Mapped[str] = mapped_column(String(100), nullable=False)
    registry_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class JobRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        CheckConstraint("trigger IN ('automatic','manual')", name="trigger_valid"),
        CheckConstraint("kind IN ('function','projection')", name="kind_valid"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','partial','failed','cancelled')",
            name="status_valid",
        ),
        Index(
            "uq_job_runs_automatic_key_edition",
            "automatic_key",
            "edition_date",
            unique=True,
            postgresql_where=text("trigger = 'automatic'"),
        ),
        Index("ix_job_runs_claim", "status", "next_attempt_at", "created_at"),
        Index("ix_job_runs_routine", "routine_run_id", "created_at"),
    )

    routine_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("routine_runs.id", ondelete="RESTRICT")
    )
    job_key: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    automatic_key: Mapped[str | None] = mapped_column(String(100))
    registry_version: Mapped[str] = mapped_column(String(100), nullable=False)
    registry_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class JobDependency(Base):
    __tablename__ = "job_dependencies"
    __table_args__ = (CheckConstraint("policy IN ('success','terminal')", name="policy_valid"),)

    upstream_job_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_runs.id", ondelete="CASCADE"), primary_key=True
    )
    downstream_job_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_runs.id", ondelete="CASCADE"), primary_key=True
    )
    policy: Mapped[str] = mapped_column(String(20), nullable=False)


class FunctionRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "function_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','retry_wait','succeeded','no_change','partial',"
            "'unavailable','failed','cancelled')",
            name="status_valid",
        ),
        UniqueConstraint("job_run_id", "function_key", name="uq_function_run_job_function"),
        Index("ix_function_runs_ready", "status", "next_attempt_at", "provider_key"),
    )

    job_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_runs.id", ondelete="CASCADE"), nullable=False
    )
    function_key: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    missing_scopes: Mapped[list[str] | None] = mapped_column(JSONB)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class FunctionDependency(Base):
    __tablename__ = "function_dependencies"
    __table_args__ = (CheckConstraint("policy IN ('success','terminal')", name="policy_valid"),)

    upstream_function_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("function_runs.id", ondelete="CASCADE"), primary_key=True
    )
    downstream_function_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("function_runs.id", ondelete="CASCADE"), primary_key=True
    )
    policy: Mapped[str] = mapped_column(String(20), nullable=False)


class FunctionAttempt(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "function_attempts"
    __table_args__ = (
        CheckConstraint("attempt_number > 0", name="attempt_number_positive"),
        CheckConstraint(
            "status IN ('running','succeeded','no_change','partial','unavailable',"
            "'failed','cancelled')",
            name="status_valid",
        ),
        CheckConstraint(
            "record_count IS NULL OR record_count >= 0", name="record_count_nonnegative"
        ),
        UniqueConstraint("function_run_id", "attempt_number", name="uq_function_attempt_number"),
        Index("ix_function_attempts_provider_started", "provider_key", "started_at"),
    )

    function_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("function_runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_key: Mapped[str] = mapped_column(String(100), nullable=False)
    function_key: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    fence_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="running")
    request_metadata: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_as_of: Mapped[date | None] = mapped_column(Date)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    record_count: Mapped[int | None] = mapped_column(Integer)
    payload_digest: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(String(500))


class ProjectionInputFreeze(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "projection_input_freezes"
    __table_args__ = (
        UniqueConstraint("projection_job_run_id", name="uq_projection_input_freeze_job"),
        CheckConstraint("char_length(input_digest) = 64", name="input_digest_sha256"),
    )

    projection_job_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("job_runs.id", ondelete="RESTRICT"), nullable=False
    )
    registry_version: Mapped[str] = mapped_column(String(100), nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MarketDailySeries(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "market_daily_series"
    __table_args__ = (
        UniqueConstraint("provider_key", "dataset_key", "symbol", name="uq_market_daily_series"),
    )

    provider_key: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_key: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(100), nullable=False)
    market: Mapped[str] = mapped_column(String(50), nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MarketDailyObservation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "market_daily_observations"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("char_length(value_digest) = 64", name="value_digest_sha256"),
        UniqueConstraint(
            "series_id", "observation_date", "version", name="uq_market_observation_version"
        ),
        Index("ix_market_observation_latest", "series_id", "observation_date", "version"),
    )

    series_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_daily_series.id", ondelete="RESTRICT"),
        nullable=False,
    )
    function_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("function_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    observation_date: Mapped[date] = mapped_column(Date, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    open: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    high: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    low: Mapped[Decimal | None] = mapped_column(Numeric(24, 10))
    close: Mapped[Decimal] = mapped_column(Numeric(24, 10), nullable=False)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class InterestRateSeries(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "interest_rate_series"
    __table_args__ = (
        UniqueConstraint("provider_key", "dataset_key", "symbol", name="uq_interest_rate_series"),
    )

    provider_key: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_key: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(100), nullable=False)
    market: Mapped[str] = mapped_column(String(50), nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class InterestRateObservation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "interest_rate_observations"
    __table_args__ = (
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("char_length(value_digest) = 64", name="value_digest_sha256"),
        UniqueConstraint(
            "series_id", "observation_date", "version", name="uq_interest_rate_observation_version"
        ),
        Index("ix_interest_rate_observation_latest", "series_id", "observation_date", "version"),
    )

    series_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("interest_rate_series.id", ondelete="RESTRICT"),
        nullable=False,
    )
    function_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("function_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    observation_date: Mapped[date] = mapped_column(Date, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ProjectionInputObservation(Base):
    __tablename__ = "projection_input_observations"

    freeze_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projection_input_freezes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    observation_kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    observation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)


class PublicationFunctionAttempt(Base):
    __tablename__ = "publication_function_attempts"

    publication_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("report_publications.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    function_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("function_attempts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
