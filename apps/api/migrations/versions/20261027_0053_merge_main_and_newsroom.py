"""Join main's migrations with the newsroom branch again.

Revision ID: 20261027_0053
Revises: 20261027_0052, 20261012_0034

Both lines continue from 20261008_0033. Main added the sector heatmap image
ledger (20261012_0034); the newsroom branch joined main once already
(20261019_0050) and went on to 20261027_0052. The heatmap table touches no
newsroom table, so this revision only joins the two heads: a database on either
head upgrades to here.
"""

from collections.abc import Sequence

revision: str = "20261027_0053"
down_revision: str | tuple[str, ...] | None = ("20261027_0052", "20261012_0034")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
