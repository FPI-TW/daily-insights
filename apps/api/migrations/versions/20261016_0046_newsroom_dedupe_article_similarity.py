"""Record how close two proposed duplicates' articles are.

Revision ID: 20261016_0046
Revises: 20261015_0045

The duplicate screen now also merges a pair whose mean article embeddings are
close, because one story told from several angles (an earnings call's revenue,
guidance and capex pieces) gets titles too different to match. The similarity
is kept with every proposal so the bar can be recalibrated from stored rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261016_0046"
down_revision: str | None = "20261015_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "newsroom_dedupe_decisions", sa.Column("article_similarity", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("newsroom_dedupe_decisions", "article_similarity")
