"""Index newsroom articles for the 30-day body purge.

Revision ID: 20261001_0033
Revises: 20261001_0032
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261001_0033"
down_revision: str | None = "20261001_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_newsroom_articles_purge",
        "newsroom_articles",
        ["body_status", "first_seen_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_newsroom_articles_purge", table_name="newsroom_articles")
