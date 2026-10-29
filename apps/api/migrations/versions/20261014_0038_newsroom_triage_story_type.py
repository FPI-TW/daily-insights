"""Store triage's story type and raw judgements on each article.

Revision ID: 20261014_0038
Revises: 20261013_0037

Triage now answers closed questions (story type, impact, new facts, directness
per market) and ``scoring.py`` computes ``market_scores`` from them, so the caps
for price-action and opinion pieces are enforced by code. ``story_type`` lets
assembly leave price-action pieces out of the selection; ``triage_signals`` keeps
the inputs so the formula can be recalibrated without new model calls.

Both columns are NULL for articles triaged earlier; those keep the scores the
model wrote and stay selectable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261014_0038"
down_revision: str | None = "20261013_0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("newsroom_articles", sa.Column("story_type", sa.String(length=20), nullable=True))
    op.add_column(
        "newsroom_articles",
        sa.Column("triage_signals", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_newsroom_articles_story_type_valid"),
        "newsroom_articles",
        "story_type IS NULL OR story_type IN ('official','data','catalyst','price_action',"
        "'preview','opinion','press_release','other')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_newsroom_articles_story_type_valid"), "newsroom_articles", type_="check"
    )
    op.drop_column("newsroom_articles", "triage_signals")
    op.drop_column("newsroom_articles", "story_type")
