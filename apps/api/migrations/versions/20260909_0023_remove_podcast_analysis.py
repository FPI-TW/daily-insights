"""Remove Podcast transcription and analysis state.

Revision ID: 20260909_0023
Revises: 20260909_0022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260909_0023"
down_revision: str | None = "20260909_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VARIANTS = "podcast_episode_audio_variants"


def upgrade() -> None:
    # Preserve generated titles and chapters as editable content after the
    # analysis feature and its provenance vocabulary are removed.
    op.execute(
        "UPDATE podcast_episodes SET metadata_source = 'manual' WHERE metadata_source = 'ai'"
    )
    op.drop_constraint("chapters_source_valid", VARIANTS, type_="check")
    op.execute(f"UPDATE {VARIANTS} SET chapters_source = 'manual' WHERE chapters_source = 'ai'")
    op.create_check_constraint(
        "chapters_source_valid", VARIANTS, "chapters_source IN ('none', 'file', 'manual')"
    )

    op.drop_constraint("analysis_status_valid", VARIANTS, type_="check")
    op.drop_column(VARIANTS, "transcript")
    op.drop_column(VARIANTS, "analyzed_at")
    op.drop_column(VARIANTS, "analysis_error")
    op.drop_column(VARIANTS, "analysis_status")


def downgrade() -> None:
    op.add_column(
        VARIANTS,
        sa.Column("analysis_status", sa.String(length=20), nullable=False, server_default="none"),
    )
    op.add_column(VARIANTS, sa.Column("analysis_error", sa.Text(), nullable=True))
    op.add_column(VARIANTS, sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        VARIANTS,
        sa.Column("transcript", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "analysis_status_valid",
        VARIANTS,
        "analysis_status IN ('none', 'pending', 'succeeded', 'failed')",
    )
    op.drop_constraint("chapters_source_valid", VARIANTS, type_="check")
    op.create_check_constraint(
        "chapters_source_valid", VARIANTS, "chapters_source IN ('none', 'file', 'ai', 'manual')"
    )
