"""Track durable projection attempts for bounded automatic retries.

Revision ID: 20261005_0031
Revises: 20260924_0030
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261005_0031"
down_revision: str | None = "20260924_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_runs", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.execute(
        "UPDATE job_runs SET attempt_count = 1 WHERE kind = 'projection' AND started_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("job_runs", "attempt_count")
