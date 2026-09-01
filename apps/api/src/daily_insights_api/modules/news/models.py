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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.models import Base, UUIDPrimaryKeyMixin


class NewsEdition(UUIDPrimaryKeyMixin, Base):
    """Immutable daily edition. A changed source/model input creates a revision."""

    __tablename__ = "news_editions"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("char_length(input_digest) = 64", name="input_digest_sha256"),
        CheckConstraint("status IN ('complete', 'partial', 'unavailable')", name="status_valid"),
        UniqueConstraint("edition_date", "revision", name="uq_news_edition_version"),
        Index("ix_news_editions_latest", "edition_date", "revision"),
    )
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    derivation_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    caveat: Mapped[str | None] = mapped_column(String(1_000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NewsItem(UUIDPrimaryKeyMixin, Base):
    """Selected source metadata only; article bodies are deliberately never persisted."""

    __tablename__ = "news_items"
    __table_args__ = (
        CheckConstraint("rank > 0", name="rank_positive"),
        CheckConstraint(
            "topic IN ('markets','economy','companies','policy','technology','commodities')",
            name="topic_valid",
        ),
        CheckConstraint("importance BETWEEN 1 AND 5", name="importance_range"),
        CheckConstraint("char_length(content_digest) = 64", name="content_digest_sha256"),
        UniqueConstraint("edition_id", "rank", name="uq_news_item_rank"),
        UniqueConstraint("edition_id", "source_url", name="uq_news_item_url"),
    )
    edition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("news_editions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    topic: Mapped[str] = mapped_column(String(50), nullable=False)
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_headline: Mapped[str] = mapped_column(String(1_000), nullable=False)
    source_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    importance: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    numeric_facts: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)


class NewsPresentation(Base):
    __tablename__ = "news_presentations"
    __table_args__ = (CheckConstraint("locale IN ('zh-hant','zh-hans','en')", name="locale_valid"),)
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("news_items.id", ondelete="RESTRICT"), primary_key=True
    )
    locale: Mapped[str] = mapped_column(String(10), primary_key=True)
    headline: Mapped[str] = mapped_column(String(1_000), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)


class NewsGenerationAudit(UUIDPrimaryKeyMixin, Base):
    """Per-call immutable audit, containing digests and usage but no prompt/body."""

    __tablename__ = "news_generation_audits"
    __table_args__ = (
        CheckConstraint("char_length(input_digest) = 64", name="input_digest_sha256"),
        CheckConstraint("status IN ('succeeded', 'failed')", name="status_valid"),
        Index("ix_news_generation_audits_edition", "edition_id", "created_at"),
    )
    edition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("news_editions.id", ondelete="RESTRICT"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    locale: Mapped[str | None] = mapped_column(String(10))
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
