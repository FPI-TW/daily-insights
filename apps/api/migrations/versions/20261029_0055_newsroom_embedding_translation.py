"""Retain successful embedding translations independently of vector generation.

Revision ID: 20261029_0055
Revises: 20261029_0054
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261029_0055"
down_revision: str | None = "20261029_0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("newsroom_articles", sa.Column("embedding_translation_digest", sa.String(64)))
    op.add_column("newsroom_articles", sa.Column("embedding_translation_text", sa.Text()))


def downgrade() -> None:
    op.drop_column("newsroom_articles", "embedding_translation_text")
    op.drop_column("newsroom_articles", "embedding_translation_digest")
