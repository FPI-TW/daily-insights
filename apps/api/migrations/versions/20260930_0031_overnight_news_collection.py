"""Add overnight news collection pool, feed poll states and headline screening.

Revision ID: 20260930_0031
Revises: 20260924_0030
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260930_0031"
down_revision: str | None = "20260924_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CANDIDATES = "news_candidates"
PREVIOUS_STAGES = (
    "('discovered','fetch_failed','unused','reviewed','prepared','dropped','published')"
)
STAGES = (
    "('discovered','fetch_failed','unused','reviewed','prepared','dropped','published',"
    "'screened_out')"
)


def upgrade() -> None:
    op.create_table(
        "news_collected_candidates",
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("source_name", sa.String(length=100), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("markets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("first_collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(candidate_id) = 64",
            name=op.f("ck_news_collected_candidates_candidate_id_sha256"),
        ),
        sa.CheckConstraint(
            "char_length(source_key) = 64",
            name=op.f("ck_news_collected_candidates_source_key_sha256"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(markets) = 'array'",
            name=op.f("ck_news_collected_candidates_markets_array"),
        ),
        sa.PrimaryKeyConstraint("candidate_id", name=op.f("pk_news_collected_candidates")),
    )
    op.create_index(
        op.f("ix_news_collected_candidates_first_collected_at"),
        "news_collected_candidates",
        ["first_collected_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_news_collected_candidates_last_collected_at"),
        "news_collected_candidates",
        ["last_collected_at"],
        unique=False,
    )

    op.create_table(
        "news_feed_poll_states",
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("feed_url", sa.Text(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Integer(), nullable=True),
        sa.Column("last_count", sa.Integer(), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_gap_minutes", sa.Integer(), nullable=True),
        sa.Column("gap_count_since", sa.Date(), nullable=True),
        sa.Column("gap_count", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "char_length(source_key) = 64",
            name=op.f("ck_news_feed_poll_states_source_key_sha256"),
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name=op.f("ck_news_feed_poll_states_consecutive_failures_nonnegative"),
        ),
        sa.CheckConstraint(
            "gap_count >= 0", name=op.f("ck_news_feed_poll_states_gap_count_nonnegative")
        ),
        sa.PrimaryKeyConstraint("source_key", name=op.f("pk_news_feed_poll_states")),
    )

    op.add_column(CANDIDATES, sa.Column("discovered_via", sa.String(length=16), nullable=True))
    op.add_column(CANDIDATES, sa.Column("screen_rank", sa.Integer(), nullable=True))
    op.add_column(CANDIDATES, sa.Column("screen_score", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_news_candidates_discovered_via_valid"),
        CANDIDATES,
        "discovered_via IS NULL OR discovered_via IN ('live','collected','both')",
    )
    op.create_check_constraint(
        op.f("ck_news_candidates_screen_score_range"),
        CANDIDATES,
        "screen_score IS NULL OR screen_score BETWEEN 1 AND 5",
    )
    op.drop_constraint(op.f("ck_news_candidates_stage_valid"), CANDIDATES, type_="check")
    op.create_check_constraint(
        op.f("ck_news_candidates_stage_valid"), CANDIDATES, f"stage IN {STAGES}"
    )


def downgrade() -> None:
    # A headline-screened candidate was never extracted; before the screening
    # stage existed such a candidate would simply have gone unused.
    op.execute(sa.text("UPDATE news_candidates SET stage = 'unused' WHERE stage = 'screened_out'"))
    op.drop_constraint(op.f("ck_news_candidates_stage_valid"), CANDIDATES, type_="check")
    op.create_check_constraint(
        op.f("ck_news_candidates_stage_valid"), CANDIDATES, f"stage IN {PREVIOUS_STAGES}"
    )
    op.drop_constraint(op.f("ck_news_candidates_screen_score_range"), CANDIDATES, type_="check")
    op.drop_constraint(op.f("ck_news_candidates_discovered_via_valid"), CANDIDATES, type_="check")
    op.drop_column(CANDIDATES, "screen_score")
    op.drop_column(CANDIDATES, "screen_rank")
    op.drop_column(CANDIDATES, "discovered_via")

    op.drop_table("news_feed_poll_states")
    op.drop_index(
        op.f("ix_news_collected_candidates_last_collected_at"),
        table_name="news_collected_candidates",
    )
    op.drop_index(
        op.f("ix_news_collected_candidates_first_collected_at"),
        table_name="news_collected_candidates",
    )
    op.drop_table("news_collected_candidates")
