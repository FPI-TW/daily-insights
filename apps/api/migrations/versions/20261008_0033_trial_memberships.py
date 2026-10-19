"""Add trial membership deadlines; existing members remain regular."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261008_0033"
down_revision: str | None = "20261007_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memberships",
        sa.Column("member_type", sa.String(16), nullable=False, server_default="regular"),
    )
    op.add_column("memberships", sa.Column("trial_expires_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "membership_trial_valid",
        "memberships",
        "(member_type = 'regular' AND trial_expires_at IS NULL) OR "
        "(member_type = 'trial' AND trial_expires_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("membership_trial_valid", "memberships", type_="check")
    op.drop_column("memberships", "trial_expires_at")
    op.drop_column("memberships", "member_type")
