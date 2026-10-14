"""Let admins delete newsroom sources without losing their articles.

Revision ID: 20261013_0036
Revises: 20261001_0035

Articles reference their source with ``ON DELETE RESTRICT`` and keep backing
past events and published editions, so a deleted source stays as a row with
``deleted_at`` set. The admin list hides it and it can never be polled again:
a deleted source must be disabled.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261013_0036"
down_revision: str | None = "20261001_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "newsroom_sources", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_newsroom_sources_deleted_disabled"),
        "newsroom_sources",
        "deleted_at IS NULL OR NOT enabled",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_newsroom_sources_deleted_disabled"), "newsroom_sources", type_="check"
    )
    op.drop_column("newsroom_sources", "deleted_at")
