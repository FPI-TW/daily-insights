"""Newsroom tables. Every retryable stage is a set of queue columns on its own row.

A stage named ``<stage>`` owns ``<stage>_status``, ``<stage>_attempts``,
``<stage>_next_attempt_at`` and ``<stage>_error_code``; ``queue.py`` is the only
code that claims and settles them. There are no checkpoint tables: the row is the
state (docs/specs/newsroom-pipeline.md §5).
"""

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column

from daily_insights_api.core.models import Base, TimestampMixin, UUIDPrimaryKeyMixin

EMBEDDING_DIMENSIONS = 1536
BODY_MAX_CHARS = 40_000

MARKET_CODES = ("global", "tw_equity", "us_equity")
TOPICS = ("markets", "economy", "companies", "policy", "technology", "commodities")
SOURCE_KINDS = (
    "rss",
    "rdf",
    "atom",
    "rss_full",
    "news_sitemap",
    "json_list",
    "guardian_api",
    "manual",
)
BODY_STATUSES = ("pending", "ok", "unavailable", "rejected", "purged")
BODY_SOURCES = ("feed", "fetch", "manual")
QUEUE_STATUSES = ("idle", "pending", "done", "failed")
ANALYSIS_STATUSES = ("idle", "pending", "ready", "failed", "needs_body")
TRANSLATION_STATUSES = ("idle", "pending", "ready", "failed")
WHY_STATUSES = ("pending", "ready", "failed")
EVENT_STATUSES = ("open", "merged")
EVENT_ORIGINS = ("triage", "split", "manual", "legacy")
EDITION_STATUSES = ("draft", "published")
SELECTION_MODES = ("pending", "editor", "fallback", "legacy")
ITEM_ORIGINS = ("model", "manual", "legacy")
LLM_STAGES = ("embed", "triage", "editor", "analysis", "why", "translate")
EDIT_ENTITY_TYPES = ("source", "article", "event", "edition", "item")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ",".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _queue_columns_check(stage: str, statuses: tuple[str, ...]) -> tuple[CheckConstraint, ...]:
    return (
        CheckConstraint(_in(f"{stage}_status", statuses), name=f"{stage}_status_valid"),
        CheckConstraint(f"{stage}_attempts >= 0", name=f"{stage}_attempts_nonnegative"),
    )


class NewsroomSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A publisher feed managed from the admin console (spec §4.1)."""

    __tablename__ = "newsroom_sources"
    __table_args__ = (
        CheckConstraint(_in("kind", SOURCE_KINDS), name="kind_valid"),
        CheckConstraint("trust_tier BETWEEN 1 AND 3", name="trust_tier_range"),
        CheckConstraint("weight >= 0.5 AND weight <= 2.0", name="weight_range"),
        CheckConstraint("poll_interval_minutes BETWEEN 5 AND 1440", name="poll_interval_range"),
        CheckConstraint("consecutive_failures >= 0", name="failures_nonnegative"),
        CheckConstraint(
            "markets <@ ARRAY['global','tw_equity','us_equity']::varchar[]", name="markets_valid"
        ),
        UniqueConstraint("key", name="uq_newsroom_sources_key"),
        Index("ix_newsroom_sources_due", "enabled", "next_poll_at"),
    )
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    # Optional regex an article link must match to count as this publisher's story.
    link_pattern: Mapped[str | None] = mapped_column(String(500))
    markets: Mapped[list[str]] = mapped_column(ARRAY(String(20)), nullable=False, default=list)
    language_filter: Mapped[list[str] | None] = mapped_column(ARRAY(String(10)))
    full_text_in_feed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Adapter-specific options carried over from the legacy feed registry.
    options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    trust_tier: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0, server_default="1")
    poll_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the unhealthy notification fired; cleared on the next success so
    # an outage notifies once.
    unhealthy_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NewsroomEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One story inside one edition window, plus its shared analysis (spec §4.3)."""

    __tablename__ = "newsroom_events"
    __table_args__ = (
        CheckConstraint(_in("status", EVENT_STATUSES), name="status_valid"),
        CheckConstraint(_in("created_by", EVENT_ORIGINS), name="created_by_valid"),
        CheckConstraint(
            "(status = 'merged') = (merged_into_id IS NOT NULL)", name="merged_target_consistent"
        ),
        *_queue_columns_check("analysis", ANALYSIS_STATUSES),
        *_queue_columns_check("en", TRANSLATION_STATUSES),
        Index("ix_newsroom_events_window", "edition_date", "status"),
        Index("ix_newsroom_events_analysis_due", "analysis_status", "analysis_next_attempt_at"),
        Index("ix_newsroom_events_en_due", "en_status", "en_next_attempt_at"),
    )
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    working_title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="open", server_default="open"
    )
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("newsroom_events.id", ondelete="RESTRICT")
    )
    created_by: Mapped[str] = mapped_column(
        String(10), nullable=False, default="triage", server_default="triage"
    )
    headline_zh_hant: Mapped[str | None] = mapped_column(String(500))
    summary_zh_hant: Mapped[str | None] = mapped_column(Text)
    headline_zh_hans: Mapped[str | None] = mapped_column(String(500))
    summary_zh_hans: Mapped[str | None] = mapped_column(Text)
    headline_en: Mapped[str | None] = mapped_column(String(500))
    summary_en: Mapped[str | None] = mapped_column(Text)
    # [{"symbol": "^TWII", "kind": "index", "label": "加權指數"}]; only symbols the
    # site has a dashboard for survive analysis.
    related_symbols: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    analysis_status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="idle", server_default="idle"
    )
    analysis_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    analysis_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis_error_code: Mapped[str | None] = mapped_column(String(100))
    analysis_model: Mapped[str | None] = mapped_column(String(200))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    en_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="idle", server_default="idle"
    )
    en_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    en_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    en_error_code: Mapped[str | None] = mapped_column(String(100))
    # Digest of the zh-hant text (event + its items' why) the English came from.
    en_source_digest: Mapped[str | None] = mapped_column(String(64))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )


class NewsroomArticle(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One publisher URL. The body is internal-only and purged after 30 days (spec §4.2)."""

    __tablename__ = "newsroom_articles"
    __table_args__ = (
        CheckConstraint("char_length(url_hash) = 64", name="url_hash_sha256"),
        CheckConstraint(_in("body_status", BODY_STATUSES), name="body_status_valid"),
        CheckConstraint(
            f"body_source IS NULL OR {_in('body_source', BODY_SOURCES)}", name="body_source_valid"
        ),
        CheckConstraint(
            f"body IS NULL OR char_length(body) <= {BODY_MAX_CHARS}", name="body_length"
        ),
        CheckConstraint(f"topic IS NULL OR {_in('topic', TOPICS)}", name="topic_valid"),
        *_queue_columns_check("fetch", QUEUE_STATUSES),
        *_queue_columns_check("embed", QUEUE_STATUSES),
        *_queue_columns_check("triage", QUEUE_STATUSES),
        UniqueConstraint("url_hash", name="uq_newsroom_articles_url_hash"),
        Index("ix_newsroom_articles_window", "edition_date", "event_id"),
        Index("ix_newsroom_articles_fetch_due", "fetch_status", "fetch_next_attempt_at"),
        Index("ix_newsroom_articles_embed_due", "embed_status", "embed_next_attempt_at"),
        Index("ix_newsroom_articles_triage_due", "triage_status", "triage_next_attempt_at"),
        Index("ix_newsroom_articles_event", "event_id"),
        # The 30-day body purge scans by status and age.
        Index("ix_newsroom_articles_purge", "body_status", "first_seen_at"),
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("newsroom_sources.id", ondelete="RESTRICT"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(1_000), nullable=False)
    feed_summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    language: Mapped[str | None] = mapped_column(String(10))
    body: Mapped[str | None] = mapped_column(Text)
    body_status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="pending", server_default="pending"
    )
    body_source: Mapped[str | None] = mapped_column(String(10))
    body_quality_reason: Mapped[str | None] = mapped_column(String(100))
    body_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetch_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pending", server_default="pending"
    )
    fetch_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    fetch_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetch_error_code: Mapped[str | None] = mapped_column(String(100))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    embed_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="idle", server_default="idle"
    )
    embed_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    embed_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embed_error_code: Mapped[str | None] = mapped_column(String(100))
    triage_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="idle", server_default="idle"
    )
    triage_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    triage_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triage_error_code: Mapped[str | None] = mapped_column(String(100))
    relevant: Mapped[bool | None] = mapped_column(Boolean)
    topic: Mapped[str | None] = mapped_column(String(20))
    # {"global": 0-100, "tw_equity": 0-100, "us_equity": 0-100}
    market_scores: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("newsroom_events.id", ondelete="RESTRICT")
    )
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Admin who submitted an off-pool URL (manual source only).
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )


class NewsroomEdition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The single mutable edition for one date and market (spec §4.4)."""

    __tablename__ = "newsroom_editions"
    __table_args__ = (
        CheckConstraint(_in("market_code", MARKET_CODES), name="market_code_valid"),
        CheckConstraint(_in("status", EDITION_STATUSES), name="status_valid"),
        CheckConstraint(_in("selection_mode", SELECTION_MODES), name="selection_mode_valid"),
        CheckConstraint(
            "(status = 'published') = (published_at IS NOT NULL)", name="published_consistent"
        ),
        CheckConstraint("ignored_pending_triage >= 0", name="ignored_nonnegative"),
        UniqueConstraint("edition_date", "market_code", name="uq_newsroom_edition_date_market"),
        Index("ix_newsroom_editions_due", "status", "auto_publish_at"),
    )
    edition_date: Mapped[date] = mapped_column(Date, nullable=False)
    market_code: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="draft", server_default="draft"
    )
    selection_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pending", server_default="pending"
    )
    auto_publish_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    late_fill_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    assembled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ignored_pending_triage: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL on a published edition means the 09:00 auto-publish released it.
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    # Set once the 12:00 late-fill sweep has run for this edition.
    late_fill_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NewsroomEditionItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One event placed in one market edition, with that market's "why it matters"."""

    __tablename__ = "newsroom_edition_items"
    __table_args__ = (
        CheckConstraint("rank > 0", name="rank_positive"),
        CheckConstraint("stars IS NULL OR stars BETWEEN 1 AND 5", name="stars_range"),
        CheckConstraint(_in("origin", ITEM_ORIGINS), name="origin_valid"),
        *_queue_columns_check("why", WHY_STATUSES),
        CheckConstraint(_in("why_en_status", TRANSLATION_STATUSES), name="why_en_status_valid"),
        UniqueConstraint("edition_id", "event_id", name="uq_newsroom_item_edition_event"),
        Index("ix_newsroom_items_edition", "edition_id", "rank"),
        Index("ix_newsroom_items_why_due", "why_status", "why_next_attempt_at"),
        Index("ix_newsroom_items_event", "event_id"),
    )
    edition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("newsroom_editions.id", ondelete="RESTRICT"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("newsroom_events.id", ondelete="RESTRICT"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    stars: Mapped[int | None] = mapped_column(Integer)
    # Event score for this market when the edition was assembled (spec §6.3 step 2).
    editor_score: Mapped[float | None] = mapped_column(Float)
    origin: Mapped[str] = mapped_column(
        String(10), nullable=False, default="model", server_default="model"
    )
    why_zh_hant: Mapped[str | None] = mapped_column(Text)
    why_zh_hans: Mapped[str | None] = mapped_column(Text)
    why_en: Mapped[str | None] = mapped_column(Text)
    why_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pending", server_default="pending"
    )
    why_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    why_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    why_error_code: Mapped[str | None] = mapped_column(String(100))
    why_en_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="idle", server_default="idle"
    )
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hidden_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NewsroomEditLog(UUIDPrimaryKeyMixin, Base):
    """Append-only record of every admin action (spec §4.6)."""

    __tablename__ = "newsroom_edit_log"
    __table_args__ = (
        CheckConstraint(_in("entity_type", EDIT_ENTITY_TYPES), name="entity_type_valid"),
        Index("ix_newsroom_edit_log_entity", "entity_type", "entity_id", "created_at"),
    )
    entity_type: Mapped[str] = mapped_column(String(10), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NewsroomLlmCall(UUIDPrimaryKeyMixin, Base):
    """Audit row per model or embedding call; prompts and bodies are never stored."""

    __tablename__ = "newsroom_llm_calls"
    __table_args__ = (
        CheckConstraint(_in("stage", LLM_STAGES), name="stage_valid"),
        Index("ix_newsroom_llm_calls_stage_time", "stage", "created_at"),
    )
    stage: Mapped[str] = mapped_column(String(10), nullable=False)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(100))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ``metadata.create_all`` (tests, local bootstrap) needs the extension before the
# vector column; production gets it from migration 20261001_0031.
def _ensure_vector_extension(target: Any, connection: Connection, **kwargs: Any) -> None:
    del target, kwargs
    connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")


event.listen(NewsroomArticle.__table__, "before_create", _ensure_vector_extension)
