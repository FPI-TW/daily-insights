import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ReportPipelineRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "report_pipeline_runs"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("char_length(idempotency_key) = 64", name="idempotency_key_sha256"),
        CheckConstraint(
            "status IN ('pending', 'running', 'published', 'failed')",
            name="status_valid",
        ),
        CheckConstraint(
            "(status = 'pending' AND started_at IS NULL AND finished_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'published' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND error_code IS NULL) OR "
            "(status = 'failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name="status_fields_consistent",
        ),
        UniqueConstraint("idempotency_key"),
        UniqueConstraint(
            "report_key",
            "market_code",
            "edition_date",
            "revision",
            name="uq_report_pipeline_run_version",
        ),
        Index("ix_report_pipeline_runs_claim", "status", "lease_expires_at"),
        Index(
            "ix_report_pipeline_runs_market_edition",
            "market_code",
            "edition_date",
        ),
    )

    report_key: Mapped[str] = mapped_column(String(100), nullable=False)
    market_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("markets.code", ondelete="RESTRICT"), nullable=False
    )
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    derivation_version: Mapped[str] = mapped_column(String(100), nullable=False)
    content_schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(String(1_000))


class SourceRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Provider-call metadata only; provider rows and response bodies are forbidden."""

    __tablename__ = "source_runs"
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("report_pipeline_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_key: Mapped[str] = mapped_column(String(100), nullable=False)
    pipeline_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        CheckConstraint("pipeline_attempt > 0", name="pipeline_attempt_positive"),
        CheckConstraint("attempt > 0", name="attempt_positive"),
        CheckConstraint(
            "record_count IS NULL OR record_count >= 0",
            name="record_count_nonnegative",
        ),
        CheckConstraint("char_length(contract_hash) = 64", name="contract_hash_sha256"),
        CheckConstraint("char_length(request_fingerprint) = 64", name="request_fingerprint_sha256"),
        CheckConstraint(
            "payload_sha256 IS NULL OR char_length(payload_sha256) = 64",
            name="payload_digest_sha256",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="status_valid",
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND source_as_of IS NULL "
            "AND record_count IS NULL AND payload_sha256 IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND source_as_of IS NOT NULL "
            "AND record_count IS NOT NULL AND payload_sha256 IS NOT NULL "
            "AND error_code IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL "
            "AND source_as_of IS NULL AND record_count IS NULL AND payload_sha256 IS NULL)",
            name="status_fields_consistent",
        ),
        UniqueConstraint(
            "pipeline_run_id",
            "dataset_key",
            "pipeline_attempt",
            "attempt",
            name="uq_source_run_attempt",
        ),
        Index("ix_source_runs_status_started", "status", "started_at"),
        Index(
            "ix_source_runs_provenance",
            "provider",
            "dataset_key",
            "source_as_of",
            postgresql_where="status = 'succeeded'",
        ),
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running", server_default="running"
    )
    contract_version: Mapped[str] = mapped_column(String(100), nullable=False)
    contract_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    source_as_of: Mapped[date | None] = mapped_column(Date)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    record_count: Mapped[int | None] = mapped_column(Integer)
    payload_sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(String(1_000))
