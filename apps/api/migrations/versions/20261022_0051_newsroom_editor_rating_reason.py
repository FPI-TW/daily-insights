"""Keep the editor's reason with each rating.

Revision ID: 20261022_0051
Revises: 20261019_0050

Editor prompt v4 (the p0 selection variant) answers a one-sentence reason with
each rating, required when the stars and the triage impact differ by two or
more. Stored so the admin console and replays can check those disagreements.
Ratings from earlier prompts, and ratings without a reason, keep NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261022_0051"
down_revision: str | None = "20261019_0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "newsroom_editor_ratings", sa.Column("reason", sa.String(length=200), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("newsroom_editor_ratings", "reason")
