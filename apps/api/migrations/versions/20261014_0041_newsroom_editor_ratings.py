"""Keep the editor's stars for every candidate, not only the picked ones.

Revision ID: 20261014_0041
Revises: 20261014_0040

Edition items record the stars of the events that made an edition, so a strong
story that was left out left no trace of how the editor rated it. Each
assembly now appends one row per rated candidate: its place in the ranked list,
its score, its stars, whether the quota picked it and whether it reached the
editor as a follow-up of an earlier edition.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261014_0041"
down_revision: str | None = "20261014_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "newsroom_editor_ratings",
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("market_code", sa.String(length=20), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("event_title", sa.String(length=500), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False),
        sa.Column("picked", sa.Boolean(), nullable=False),
        sa.Column("follow_up", sa.Boolean(), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "market_code IN ('global','tw_equity','us_equity')",
            name=op.f("ck_newsroom_editor_ratings_market_code_valid"),
        ),
        sa.CheckConstraint(
            "stars BETWEEN 1 AND 5", name=op.f("ck_newsroom_editor_ratings_stars_range")
        ),
        sa.CheckConstraint(
            "position > 0", name=op.f("ck_newsroom_editor_ratings_position_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_newsroom_editor_ratings")),
    )
    op.create_index(
        "ix_newsroom_editor_ratings_edition",
        "newsroom_editor_ratings",
        ["edition_date", "market_code", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_newsroom_editor_ratings_edition", table_name="newsroom_editor_ratings")
    op.drop_table("newsroom_editor_ratings")
