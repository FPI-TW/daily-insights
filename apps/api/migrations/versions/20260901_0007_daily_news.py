"""Create immutable daily-news editions, localized presentations, and audits.

Revision ID: 20260901_0007
Revises: 20260830_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260901_0007"
down_revision: str | None = "20260830_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_editions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("derivation_version", sa.String(100), nullable=False),
        sa.Column("model_name", sa.String(200)),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("caveat", sa.String(1_000)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.CheckConstraint("char_length(input_digest) = 64", name="input_digest_sha256"),
        sa.CheckConstraint("status IN ('complete', 'partial', 'unavailable')", name="status_valid"),
        sa.UniqueConstraint("edition_date", "revision", name="uq_news_edition_version"),
    )
    op.create_index("ix_news_editions_latest", "news_editions", ["edition_date", "revision"])
    op.create_table(
        "news_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "edition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("news_editions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("topic", sa.String(50), nullable=False),
        sa.Column("source_name", sa.String(100), nullable=False),
        sa.Column("source_hostname", sa.String(255), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_headline", sa.String(1_000), nullable=False),
        sa.Column("source_published_at", sa.DateTime(timezone=True)),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("numeric_facts", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("rank > 0", name="rank_positive"),
        sa.CheckConstraint("importance BETWEEN 1 AND 5", name="importance_range"),
        sa.CheckConstraint("char_length(content_digest) = 64", name="content_digest_sha256"),
        sa.CheckConstraint(
            "topic IN ('markets','economy','companies','policy','technology','commodities')",
            name="topic_valid",
        ),
        sa.UniqueConstraint("edition_id", "rank", name="uq_news_item_rank"),
        sa.UniqueConstraint("edition_id", "source_url", name="uq_news_item_url"),
    )
    op.create_index("ix_news_items_edition_id", "news_items", ["edition_id"])
    op.create_table(
        "news_presentations",
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("news_items.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("locale", sa.String(10), primary_key=True),
        sa.Column("headline", sa.String(1_000), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.CheckConstraint("locale IN ('zh-hant','zh-hans','en')", name="locale_valid"),
    )
    op.create_table(
        "news_generation_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "edition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("news_editions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(50), nullable=False),
        sa.Column("locale", sa.String(10)),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider_request_id", sa.String(255)),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("error_code", sa.String(100)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("char_length(input_digest) = 64", name="input_digest_sha256"),
        sa.CheckConstraint("status IN ('succeeded', 'failed')", name="status_valid"),
    )
    op.create_index(
        "ix_news_generation_audits_edition", "news_generation_audits", ["edition_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("news_generation_audits")
    op.drop_table("news_presentations")
    op.drop_table("news_items")
    op.drop_table("news_editions")
