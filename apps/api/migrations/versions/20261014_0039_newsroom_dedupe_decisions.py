"""Record every duplicate the model proposes at assembly and what became of it.

Revision ID: 20261014_0039
Revises: 20261014_0038

Assembly merges candidate events the model groups as one story only when their
working titles are also near-identical, and (new) drops candidates an earlier
edition already carried. Until now a proposal the similarity threshold blocked
left no trace, so a split story could not be told apart from one the model never
grouped. Each proposed pair is now a row: both titles, the similarity, the
threshold in force, whether the titles share a key figure, and the outcome.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261014_0039"
down_revision: str | None = "20261014_0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "newsroom_dedupe_decisions",
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("event_title", sa.String(length=500), nullable=False),
        sa.Column("matched_event_id", sa.UUID(), nullable=False),
        sa.Column("matched_title", sa.String(length=500), nullable=False),
        sa.Column("title_similarity", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("shared_figure", sa.Boolean(), nullable=False),
        sa.Column("new_facts", sa.Boolean(), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "scope IN ('window','published')",
            name=op.f("ck_newsroom_dedupe_decisions_scope_valid"),
        ),
        sa.CheckConstraint(
            "outcome IN ('merged','below_threshold','merge_failed','excluded','follow_up')",
            name=op.f("ck_newsroom_dedupe_decisions_outcome_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_newsroom_dedupe_decisions")),
    )
    op.create_index(
        "ix_newsroom_dedupe_decisions_edition",
        "newsroom_dedupe_decisions",
        ["edition_date", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_newsroom_dedupe_decisions_edition", table_name="newsroom_dedupe_decisions")
    op.drop_table("newsroom_dedupe_decisions")
