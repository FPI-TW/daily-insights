"""Add the newsroom pipeline tables and the pgvector extension.

Revision ID: 20261001_0031
Revises: 20260924_0030
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20261001_0031"
down_revision: str | None = "20260924_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # RDS PostgreSQL ships pgvector as a trusted extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "newsroom_llm_calls",
        sa.Column("stage", sa.String(length=10), nullable=False),
        sa.Column("subject_id", sa.UUID(), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=200), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "stage IN ('embed','triage','editor','analysis','why','translate')",
            name=op.f("ck_newsroom_llm_calls_stage_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_newsroom_llm_calls")),
    )
    op.create_index(
        "ix_newsroom_llm_calls_stage_time",
        "newsroom_llm_calls",
        ["stage", "created_at"],
        unique=False,
    )
    op.create_table(
        "newsroom_sources",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("link_pattern", sa.String(length=500), nullable=True),
        sa.Column("markets", postgresql.ARRAY(sa.String(length=20)), nullable=False),
        sa.Column("language_filter", postgresql.ARRAY(sa.String(length=10)), nullable=True),
        sa.Column("full_text_in_feed", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "options", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("trust_tier", sa.Integer(), server_default="2", nullable=False),
        sa.Column("weight", sa.Float(), server_default="1", nullable=False),
        sa.Column("poll_interval_minutes", sa.Integer(), server_default="30", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unhealthy_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('rss','rdf','atom','rss_full','news_sitemap',"
            "'json_list','guardian_api','manual')",
            name=op.f("ck_newsroom_sources_kind_valid"),
        ),
        sa.CheckConstraint(
            "markets <@ ARRAY['global','tw_equity','us_equity']::varchar[]",
            name=op.f("ck_newsroom_sources_markets_valid"),
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0", name=op.f("ck_newsroom_sources_failures_nonnegative")
        ),
        sa.CheckConstraint(
            "poll_interval_minutes BETWEEN 5 AND 1440",
            name=op.f("ck_newsroom_sources_poll_interval_range"),
        ),
        sa.CheckConstraint(
            "trust_tier BETWEEN 1 AND 3", name=op.f("ck_newsroom_sources_trust_tier_range")
        ),
        sa.CheckConstraint(
            "weight >= 0.5 AND weight <= 2.0", name=op.f("ck_newsroom_sources_weight_range")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_newsroom_sources")),
        sa.UniqueConstraint("key", name="uq_newsroom_sources_key"),
    )
    op.create_index(
        "ix_newsroom_sources_due", "newsroom_sources", ["enabled", "next_poll_at"], unique=False
    )
    op.create_table(
        "newsroom_edit_log",
        sa.Column("entity_type", sa.String(length=10), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "entity_type IN ('source','article','event','edition','item')",
            name=op.f("ck_newsroom_edit_log_entity_type_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_newsroom_edit_log_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_newsroom_edit_log")),
    )
    op.create_index(
        "ix_newsroom_edit_log_entity",
        "newsroom_edit_log",
        ["entity_type", "entity_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "newsroom_editions",
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("market_code", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="draft", nullable=False),
        sa.Column("selection_mode", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column("auto_publish_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("late_fill_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assembled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ignored_pending_triage", sa.Integer(), server_default="0", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by_user_id", sa.UUID(), nullable=True),
        sa.Column("late_fill_closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'published') = (published_at IS NOT NULL)",
            name=op.f("ck_newsroom_editions_published_consistent"),
        ),
        sa.CheckConstraint(
            "market_code IN ('global','tw_equity','us_equity')",
            name=op.f("ck_newsroom_editions_market_code_valid"),
        ),
        sa.CheckConstraint(
            "selection_mode IN ('pending','editor','fallback','legacy')",
            name=op.f("ck_newsroom_editions_selection_mode_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('draft','published')", name=op.f("ck_newsroom_editions_status_valid")
        ),
        sa.CheckConstraint(
            "ignored_pending_triage >= 0", name=op.f("ck_newsroom_editions_ignored_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["published_by_user_id"],
            ["users.id"],
            name=op.f("fk_newsroom_editions_published_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_newsroom_editions")),
        sa.UniqueConstraint("edition_date", "market_code", name="uq_newsroom_edition_date_market"),
    )
    op.create_index(
        "ix_newsroom_editions_due", "newsroom_editions", ["status", "auto_publish_at"], unique=False
    )
    op.create_table(
        "newsroom_events",
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("working_title", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="open", nullable=False),
        sa.Column("merged_into_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.String(length=10), server_default="triage", nullable=False),
        sa.Column("headline_zh_hant", sa.String(length=500), nullable=True),
        sa.Column("summary_zh_hant", sa.Text(), nullable=True),
        sa.Column("headline_zh_hans", sa.String(length=500), nullable=True),
        sa.Column("summary_zh_hans", sa.Text(), nullable=True),
        sa.Column("headline_en", sa.String(length=500), nullable=True),
        sa.Column("summary_en", sa.Text(), nullable=True),
        sa.Column(
            "related_symbols",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("analysis_status", sa.String(length=12), server_default="idle", nullable=False),
        sa.Column("analysis_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("analysis_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("analysis_error_code", sa.String(length=100), nullable=True),
        sa.Column("analysis_model", sa.String(length=200), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("en_status", sa.String(length=10), server_default="idle", nullable=False),
        sa.Column("en_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("en_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("en_error_code", sa.String(length=100), nullable=True),
        sa.Column("en_source_digest", sa.String(length=64), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("edited_by_user_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'merged') = (merged_into_id IS NOT NULL)",
            name=op.f("ck_newsroom_events_merged_target_consistent"),
        ),
        sa.CheckConstraint(
            "analysis_status IN ('idle','pending','ready','failed','needs_body')",
            name=op.f("ck_newsroom_events_analysis_status_valid"),
        ),
        sa.CheckConstraint(
            "created_by IN ('triage','split','manual','legacy')",
            name=op.f("ck_newsroom_events_created_by_valid"),
        ),
        sa.CheckConstraint(
            "en_status IN ('idle','pending','ready','failed')",
            name=op.f("ck_newsroom_events_en_status_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('open','merged')", name=op.f("ck_newsroom_events_status_valid")
        ),
        sa.CheckConstraint(
            "analysis_attempts >= 0", name=op.f("ck_newsroom_events_analysis_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "en_attempts >= 0", name=op.f("ck_newsroom_events_en_attempts_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["edited_by_user_id"],
            ["users.id"],
            name=op.f("fk_newsroom_events_edited_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_id"],
            ["newsroom_events.id"],
            name=op.f("fk_newsroom_events_merged_into_id_newsroom_events"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_newsroom_events")),
    )
    op.create_index(
        "ix_newsroom_events_analysis_due",
        "newsroom_events",
        ["analysis_status", "analysis_next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_newsroom_events_en_due",
        "newsroom_events",
        ["en_status", "en_next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_newsroom_events_window", "newsroom_events", ["edition_date", "status"], unique=False
    )
    op.create_table(
        "newsroom_articles",
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("feed_summary", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("body_status", sa.String(length=12), server_default="pending", nullable=False),
        sa.Column("body_source", sa.String(length=10), nullable=True),
        sa.Column("body_quality_reason", sa.String(length=100), nullable=True),
        sa.Column("body_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetch_status", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column("fetch_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fetch_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetch_error_code", sa.String(length=100), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("embed_status", sa.String(length=10), server_default="idle", nullable=False),
        sa.Column("embed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("embed_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embed_error_code", sa.String(length=100), nullable=True),
        sa.Column("triage_status", sa.String(length=10), server_default="idle", nullable=False),
        sa.Column("triage_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("triage_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triage_error_code", sa.String(length=100), nullable=True),
        sa.Column("relevant", sa.Boolean(), nullable=True),
        sa.Column("topic", sa.String(length=20), nullable=True),
        sa.Column(
            "market_scores",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("event_id", sa.UUID(), nullable=True),
        sa.Column("triaged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_by_user_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "body_source IS NULL OR body_source IN ('feed','fetch','manual')",
            name=op.f("ck_newsroom_articles_body_source_valid"),
        ),
        sa.CheckConstraint(
            "body_status IN ('pending','ok','unavailable','rejected','purged')",
            name=op.f("ck_newsroom_articles_body_status_valid"),
        ),
        sa.CheckConstraint(
            "embed_status IN ('idle','pending','done','failed')",
            name=op.f("ck_newsroom_articles_embed_status_valid"),
        ),
        sa.CheckConstraint(
            "fetch_status IN ('idle','pending','done','failed')",
            name=op.f("ck_newsroom_articles_fetch_status_valid"),
        ),
        sa.CheckConstraint(
            "topic IS NULL OR topic IN "
            "('markets','economy','companies','policy','technology','commodities')",
            name=op.f("ck_newsroom_articles_topic_valid"),
        ),
        sa.CheckConstraint(
            "triage_status IN ('idle','pending','done','failed')",
            name=op.f("ck_newsroom_articles_triage_status_valid"),
        ),
        sa.CheckConstraint(
            "body IS NULL OR char_length(body) <= 40000",
            name=op.f("ck_newsroom_articles_body_length"),
        ),
        sa.CheckConstraint(
            "char_length(url_hash) = 64", name=op.f("ck_newsroom_articles_url_hash_sha256")
        ),
        sa.CheckConstraint(
            "embed_attempts >= 0", name=op.f("ck_newsroom_articles_embed_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "fetch_attempts >= 0", name=op.f("ck_newsroom_articles_fetch_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "triage_attempts >= 0", name=op.f("ck_newsroom_articles_triage_attempts_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["newsroom_events.id"],
            name=op.f("fk_newsroom_articles_event_id_newsroom_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["newsroom_sources.id"],
            name=op.f("fk_newsroom_articles_source_id_newsroom_sources"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"],
            ["users.id"],
            name=op.f("fk_newsroom_articles_submitted_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_newsroom_articles")),
        sa.UniqueConstraint("url_hash", name="uq_newsroom_articles_url_hash"),
    )
    op.create_index(
        "ix_newsroom_articles_embed_due",
        "newsroom_articles",
        ["embed_status", "embed_next_attempt_at"],
        unique=False,
    )
    op.create_index("ix_newsroom_articles_event", "newsroom_articles", ["event_id"], unique=False)
    op.create_index(
        "ix_newsroom_articles_fetch_due",
        "newsroom_articles",
        ["fetch_status", "fetch_next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_newsroom_articles_triage_due",
        "newsroom_articles",
        ["triage_status", "triage_next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_newsroom_articles_window",
        "newsroom_articles",
        ["edition_date", "event_id"],
        unique=False,
    )
    op.create_table(
        "newsroom_edition_items",
        sa.Column("edition_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("stars", sa.Integer(), nullable=True),
        sa.Column("editor_score", sa.Float(), nullable=True),
        sa.Column("origin", sa.String(length=10), server_default="model", nullable=False),
        sa.Column("why_zh_hant", sa.Text(), nullable=True),
        sa.Column("why_zh_hans", sa.Text(), nullable=True),
        sa.Column("why_en", sa.Text(), nullable=True),
        sa.Column("why_status", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column("why_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("why_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("why_error_code", sa.String(length=100), nullable=True),
        sa.Column("why_en_status", sa.String(length=10), server_default="idle", nullable=False),
        sa.Column("added_by_user_id", sa.UUID(), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hidden_by_user_id", sa.UUID(), nullable=True),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "origin IN ('model','manual','legacy')",
            name=op.f("ck_newsroom_edition_items_origin_valid"),
        ),
        sa.CheckConstraint(
            "why_en_status IN ('idle','pending','ready','failed')",
            name=op.f("ck_newsroom_edition_items_why_en_status_valid"),
        ),
        sa.CheckConstraint(
            "why_status IN ('pending','ready','failed')",
            name=op.f("ck_newsroom_edition_items_why_status_valid"),
        ),
        sa.CheckConstraint("rank > 0", name=op.f("ck_newsroom_edition_items_rank_positive")),
        sa.CheckConstraint(
            "stars IS NULL OR stars BETWEEN 1 AND 5",
            name=op.f("ck_newsroom_edition_items_stars_range"),
        ),
        sa.CheckConstraint(
            "why_attempts >= 0", name=op.f("ck_newsroom_edition_items_why_attempts_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["added_by_user_id"],
            ["users.id"],
            name=op.f("fk_newsroom_edition_items_added_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["newsroom_editions.id"],
            name=op.f("fk_newsroom_edition_items_edition_id_newsroom_editions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["newsroom_events.id"],
            name=op.f("fk_newsroom_edition_items_event_id_newsroom_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["hidden_by_user_id"],
            ["users.id"],
            name=op.f("fk_newsroom_edition_items_hidden_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_newsroom_edition_items")),
        sa.UniqueConstraint("edition_id", "event_id", name="uq_newsroom_item_edition_event"),
    )
    op.create_index(
        "ix_newsroom_items_edition", "newsroom_edition_items", ["edition_id", "rank"], unique=False
    )
    op.create_index("ix_newsroom_items_event", "newsroom_edition_items", ["event_id"], unique=False)
    op.create_index(
        "ix_newsroom_items_why_due",
        "newsroom_edition_items",
        ["why_status", "why_next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_newsroom_items_why_due", table_name="newsroom_edition_items")
    op.drop_index("ix_newsroom_items_event", table_name="newsroom_edition_items")
    op.drop_index("ix_newsroom_items_edition", table_name="newsroom_edition_items")
    op.drop_table("newsroom_edition_items")
    op.drop_index("ix_newsroom_articles_window", table_name="newsroom_articles")
    op.drop_index("ix_newsroom_articles_triage_due", table_name="newsroom_articles")
    op.drop_index("ix_newsroom_articles_fetch_due", table_name="newsroom_articles")
    op.drop_index("ix_newsroom_articles_event", table_name="newsroom_articles")
    op.drop_index("ix_newsroom_articles_embed_due", table_name="newsroom_articles")
    op.drop_table("newsroom_articles")
    op.drop_index("ix_newsroom_events_window", table_name="newsroom_events")
    op.drop_index("ix_newsroom_events_en_due", table_name="newsroom_events")
    op.drop_index("ix_newsroom_events_analysis_due", table_name="newsroom_events")
    op.drop_table("newsroom_events")
    op.drop_index("ix_newsroom_editions_due", table_name="newsroom_editions")
    op.drop_table("newsroom_editions")
    op.drop_index("ix_newsroom_edit_log_entity", table_name="newsroom_edit_log")
    op.drop_table("newsroom_edit_log")
    op.drop_index("ix_newsroom_sources_due", table_name="newsroom_sources")
    op.drop_table("newsroom_sources")
    op.drop_index("ix_newsroom_llm_calls_stage_time", table_name="newsroom_llm_calls")
    op.drop_table("newsroom_llm_calls")
    op.execute("DROP EXTENSION IF EXISTS vector")
