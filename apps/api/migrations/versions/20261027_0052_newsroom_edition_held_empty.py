"""Remember when auto-publish held an edition back for having no item.

Revision ID: 20261027_0052
Revises: 20261022_0051

From 2026-10-24 a provider outage failed every LLM stage, and the 09:00 sweep
still published three days of editions with no item. The sweep now keeps such
a draft unpublished and notifies operators once per edition; this column
records that hold so the notice is not repeated every minute or after a worker
restart. Existing editions keep NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261027_0052"
down_revision: str | None = "20261022_0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "newsroom_editions",
        sa.Column("held_empty_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("newsroom_editions", "held_empty_at")
