"""Stop polling Investing.com.

Revision ID: 20261019_0049
Revises: 20261019_0048

From the 2026-10-16 edition window to 2026-10-19, 252 Investing.com articles
put 9 (3.6%) into published events, all of which other outlets also covered;
many were Reuters copy. The site refused a growing share of article reads
(10-16: 31 of 142, 10-17: 70 of 87, 10-19: every one), so most articles
arrived without a body. The row stays and can be re-enabled from the admin
console.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261019_0049"
down_revision: str | None = "20261019_0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE newsroom_sources SET enabled = false WHERE key = 'investing-com-stocks'")


def downgrade() -> None:
    op.execute(
        "UPDATE newsroom_sources SET enabled = true "
        "WHERE key = 'investing-com-stocks' AND deleted_at IS NULL"
    )
