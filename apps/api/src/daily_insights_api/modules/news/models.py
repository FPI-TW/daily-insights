import uuid
from datetime import date, datetime
from typing import Any

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
        CheckConstraint(
            "market_code IN ('global','tw_equity','us_equity')", name="market_code_valid"
        ),
        UniqueConstraint("edition_date", "market_code", "revision", name="uq_news_edition_version"),
        Index("ix_news_editions_latest", "edition_date", "market_code", "revision"),
    )
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    # "global" is the five-story daily digest; market codes hold market news editions.
    market_code: Mapped[str] = mapped_column(
        String(50), nullable=False, default="global", server_default="global"
    )
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
        # Mirrors SelectedCandidate.market; NULL for editions generated before
        # the column existed.
        CheckConstraint(
            "market IS NULL OR market IN "
            "('global','us','asia','china','taiwan','europe','commodities','crypto')",
            name="market_valid",
        ),
        CheckConstraint("origin IN ('model','manual')", name="origin_valid"),
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
    # Selection-stage classification kept for grouping and cross-day event
    # tracking; both are null on editions persisted before migration 0012.
    market: Mapped[str | None] = mapped_column(String(20))
    event_key: Mapped[str | None] = mapped_column(String(80))
    # ``model`` items come from the pipeline's selection; ``manual`` items were
    # published from the admin candidate table. Both render identically.
    origin: Mapped[str] = mapped_column(
        String(10), nullable=False, default="model", server_default="model"
    )
    # A hidden item stays in the immutable edition but leaves the customer
    # response; the who/when is kept for the admin view and audit trail.
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hidden_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )


CANDIDATE_STAGES = ("discovered", "fetch_failed", "unused", "reviewed", "dropped", "published")
CANDIDATE_DROP_REASONS = ("off_market", "policy", "duplicate_event", "summary_failed", "reserve")


class NewsCandidate(UUIDPrimaryKeyMixin, Base):
    """Every feed candidate an edition saw and how far it got.

    The row is the admin's view of the pipeline: what was discovered, what the
    model returned and why something was not published. Article bodies are
    never stored; a manual publish re-fetches the article.
    """

    __tablename__ = "news_candidates"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('discovered','fetch_failed','unused','reviewed','dropped','published')",
            name="stage_valid",
        ),
        CheckConstraint(
            "drop_reason IS NULL OR drop_reason IN "
            "('off_market','policy','duplicate_event','summary_failed','reserve')",
            name="drop_reason_valid",
        ),
        UniqueConstraint("edition_id", "candidate_id", name="uq_news_candidate_edition"),
    )
    edition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("news_editions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # The pipeline's sha256 candidate id, unique within one edition.
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_name: Mapped[str] = mapped_column(String(100), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    headline: Mapped[str] = mapped_column(String(1_000), nullable=False)
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_digest: Mapped[str | None] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    drop_reason: Mapped[str | None] = mapped_column(String(30))
    # The model's original answer before market filtering and policy repair;
    # ai_rank is the 1-based position in the first round that returned it.
    ai_rank: Mapped[int | None] = mapped_column(Integer)
    ai_topic: Mapped[str | None] = mapped_column(String(50))
    ai_market: Mapped[str | None] = mapped_column(String(20))
    ai_importance: Mapped[int | None] = mapped_column(Integer)
    ai_event_key: Mapped[str | None] = mapped_column(String(80))
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("news_items.id", ondelete="SET NULL")
    )
    # Latest manual publish run that targeted this candidate; no foreign key
    # because run history belongs to the data_management module.
    publish_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    publish_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    publish_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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


class NewsWorkflow(UUIDPrimaryKeyMixin, Base):
    """Durable, news-only progress; run history remains owned by the queue."""

    __tablename__ = "news_workflows"
    __table_args__ = (
        UniqueConstraint("root_run_id", "market_code", name="uq_news_workflow_root_market"),
        Index("ix_news_workflows_market_date", "market_code", "edition_date"),
    )
    root_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    market_code: Mapped[str] = mapped_column(String(50), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    progress: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False, default=dict)
    failures: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class NewsCheckpoint(UUIDPrimaryKeyMixin, Base):
    """Only metadata and validated outputs, never article bodies or prompts."""

    __tablename__ = "news_checkpoints"
    __table_args__ = (
        UniqueConstraint("workflow_id", "key", name="uq_news_checkpoint_workflow_key"),
        Index("ix_news_checkpoints_expires_at", "expires_at"),
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("news_workflows.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    repairs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NewsDependencyState(Base):
    """Shared cooldown/account gate for news, independent of other model features."""

    __tablename__ = "news_dependency_states"
    scope: Mapped[str] = mapped_column(String(300), primary_key=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="ready")
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    probe_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    failures_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    newest_article_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
