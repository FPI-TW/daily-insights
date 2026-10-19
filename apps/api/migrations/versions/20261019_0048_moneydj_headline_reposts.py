"""Skip MoneyDJ's reposted headlines that carry no article.

Revision ID: 20261019_0048
Revises: 20261016_0047

MoneyDJ's real-time list (MB00) also lists other papers' headlines marked
"無內文" (no text). Their pages hold no story, only lists of other headlines,
which overlap the title well enough to pass the body quality check. The source
now excludes those titles at polling, unless an admin has already set a pattern.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261019_0048"
down_revision: str | None = "20261016_0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PATTERN = "無內文"


def upgrade() -> None:
    op.execute(
        f"UPDATE newsroom_sources SET title_exclude_pattern = '{PATTERN}' "
        "WHERE key = 'moneydj-mb00' AND title_exclude_pattern IS NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE newsroom_sources SET title_exclude_pattern = NULL "
        f"WHERE key = 'moneydj-mb00' AND title_exclude_pattern = '{PATTERN}'"
    )
