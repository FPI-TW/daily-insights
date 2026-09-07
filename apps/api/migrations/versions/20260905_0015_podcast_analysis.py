"""Track podcast analysis state, transcripts and metadata provenance.

Revision ID: 20260905_0015
Revises: 20260905_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0015"
down_revision: str | None = "20260905_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VARIANTS = "podcast_episode_audio_variants"


def upgrade() -> None:
    op.add_column(
        "podcast_episodes",
        sa.Column(
            "metadata_source", sa.String(length=20), nullable=False, server_default="derived"
        ),
    )
    op.add_column(
        VARIANTS,
        sa.Column("chapters_source", sa.String(length=20), nullable=False, server_default="none"),
    )
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
        "chapters_source_valid", VARIANTS, "chapters_source IN ('none', 'file', 'ai', 'manual')"
    )
    op.create_check_constraint(
        "analysis_status_valid",
        VARIANTS,
        "analysis_status IN ('none', 'pending', 'succeeded', 'failed')",
    )
    # Markers that already exist were read from the uploaded file.
    op.execute(
        f"UPDATE {VARIANTS} SET chapters_source = 'file' WHERE jsonb_array_length(chapters) > 0"
    )


def downgrade() -> None:
    op.drop_constraint("analysis_status_valid", VARIANTS, type_="check")
    op.drop_constraint("chapters_source_valid", VARIANTS, type_="check")
    op.drop_column(VARIANTS, "transcript")
    op.drop_column(VARIANTS, "analyzed_at")
    op.drop_column(VARIANTS, "analysis_error")
    op.drop_column(VARIANTS, "analysis_status")
    op.drop_column(VARIANTS, "chapters_source")
    op.drop_column("podcast_episodes", "metadata_source")
