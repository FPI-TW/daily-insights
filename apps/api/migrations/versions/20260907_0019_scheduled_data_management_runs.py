"""Let a scheduled data-management run have no requesting user.

Revision ID: 20260907_0019
Revises: 20260907_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260907_0019"
down_revision: str | None = "20260907_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "data_management_runs"
COLUMN = "requested_by_user_id"


def upgrade() -> None:
    # audit_events already models "no human actor" with a nullable actor; a run
    # the scheduler queued has the same shape.
    op.alter_column(TABLE, COLUMN, existing_type=sa.dialects.postgresql.UUID(), nullable=True)


def downgrade() -> None:
    # Scheduled runs cannot be attributed to anyone, so they go rather than
    # block the column. They are a log of work already done, not the work.
    op.execute(sa.text(f"DELETE FROM {TABLE} WHERE {COLUMN} IS NULL"))
    op.alter_column(TABLE, COLUMN, existing_type=sa.dialects.postgresql.UUID(), nullable=False)
