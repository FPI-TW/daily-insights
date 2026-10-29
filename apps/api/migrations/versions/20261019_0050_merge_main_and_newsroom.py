"""Join main's migrations with the newsroom branch.

Revision ID: 20261019_0050
Revises: 20261008_0033, 20261019_0049

Both lines start at 20260924_0030. Main added the projection attempt count,
the podcast removal and trial memberships (20261005_0031 to 20261008_0033);
the newsroom branch added the newsroom tables and removed the legacy news
tables (20261001_0031 to 20261019_0049). They touch different tables, so this
revision only joins the two heads: a database on either head upgrades to here.
"""

from collections.abc import Sequence

revision: str = "20261019_0050"
down_revision: str | tuple[str, ...] | None = ("20261008_0033", "20261019_0049")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
