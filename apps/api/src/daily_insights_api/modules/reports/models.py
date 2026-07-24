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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.models import Base, UUIDPrimaryKeyMixin


class ReportPublication(UUIDPrimaryKeyMixin, Base):
    """An immutable published version.

    The model deliberately has no ``updated_at`` column. Corrections create a
    later revision instead of mutating a published value.
    """

    __tablename__ = "report_publications"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("char_length(input_digest) = 64", name="input_digest_sha256"),
        CheckConstraint("jsonb_typeof(content) = 'object'", name="content_is_object"),
        CheckConstraint(
            "jsonb_typeof(presentations) = 'object' "
            "AND presentations ?& ARRAY['zh-TW', 'zh-CN', 'en'] "
            "AND presentations - ARRAY['zh-TW', 'zh-CN', 'en'] = '{}'::jsonb",
            name="presentations_have_supported_locales",
        ),
        UniqueConstraint("pipeline_run_id"),
        UniqueConstraint(
            "report_key",
            "market_code",
            "edition_date",
            "revision",
            name="uq_report_publication_version",
        ),
        Index(
            "ix_report_publications_latest",
            "report_key",
            "market_code",
            "edition_date",
            "revision",
        ),
    )

    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("report_pipeline_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    report_key: Mapped[str] = mapped_column(String(100), nullable=False)
    market_code: Mapped[str] = mapped_column(
        String(50), ForeignKey("markets.code", ondelete="RESTRICT"), nullable=False
    )
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    derivation_version: Mapped[str] = mapped_column(String(100), nullable=False)
    content_schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    content: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    presentations: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PublicationSourceRun(Base):
    """Immutable provenance edge from a publication to successful source metadata."""

    __tablename__ = "publication_source_runs"

    publication_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("report_publications.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    source_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_runs.id", ondelete="RESTRICT"),
        primary_key=True,
    )
