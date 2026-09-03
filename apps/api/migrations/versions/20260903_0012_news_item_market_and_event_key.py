"""Store the selection-stage market and event key on news items and audio duration.

Revision ID: 20260903_0012
Revises: 20260902_0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260903_0012"
down_revision: str | None = "20260902_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MARKETS = "('global','us','asia','china','taiwan','europe','commodities','crypto')"


def upgrade() -> None:
    op.add_column("news_items", sa.Column("market", sa.String(length=20), nullable=True))
    op.add_column("news_items", sa.Column("event_key", sa.String(length=80), nullable=True))
    op.create_check_constraint(
        "market_valid", "news_items", f"market IS NULL OR market IN {MARKETS}"
    )
    op.add_column(
        "podcast_episode_audio_variants",
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "duration_positive",
        "podcast_episode_audio_variants",
        "duration_seconds IS NULL OR duration_seconds > 0",
    )


def downgrade() -> None:
    op.drop_constraint("duration_positive", "podcast_episode_audio_variants", type_="check")
    op.drop_column("podcast_episode_audio_variants", "duration_seconds")
    op.drop_constraint("market_valid", "news_items", type_="check")
    op.drop_column("news_items", "event_key")
    op.drop_column("news_items", "market")
