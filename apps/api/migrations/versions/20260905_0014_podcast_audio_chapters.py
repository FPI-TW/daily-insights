"""Store chapter markers on Podcast audio variants.

Revision ID: 20260905_0014
Revises: 20260903_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0014"
down_revision: str | None = "20260903_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "podcast_episode_audio_variants",
        sa.Column(
            "chapters",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "chapters_is_array",
        "podcast_episode_audio_variants",
        "jsonb_typeof(chapters) = 'array'",
    )


def downgrade() -> None:
    op.drop_constraint("chapters_is_array", "podcast_episode_audio_variants", type_="check")
    op.drop_column("podcast_episode_audio_variants", "chapters")
