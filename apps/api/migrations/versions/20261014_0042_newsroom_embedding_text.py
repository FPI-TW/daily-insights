"""Keep the exact text each article was embedded from.

Revision ID: 20261014_0042
Revises: 20261014_0041

Non-Chinese articles are now translated to Traditional Chinese before they are
embedded (and the embedding model moves to text-embedding-3-large at 1536
dimensions), so the embedded text no longer equals the stored title and
summary. Keeping it lets a later re-embed reuse the translation, and shows why
two articles landed close together.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261014_0042"
down_revision: str | None = "20261014_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("newsroom_articles", sa.Column("embedding_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("newsroom_articles", "embedding_text")
