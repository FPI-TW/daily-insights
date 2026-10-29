"""Record article bodies read through CloakBrowser as ``body_source = browser``.

Revision ID: 20261014_0043
Revises: 20261014_0042

The fetch stage now reads a page again in CloakBrowser when the plain HTTP read
was refused as a bot or returned no article text. Marking those bodies apart
from ``fetch`` shows how many articles the browser recovers. Downgrading turns
them back into ``fetch``, which is how they were read before the distinction.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261014_0043"
down_revision: str | None = "20261014_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_newsroom_articles_body_source_valid"


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), "newsroom_articles", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT),
        "newsroom_articles",
        "body_source IS NULL OR body_source IN ('feed','fetch','browser','manual')",
    )


def downgrade() -> None:
    op.execute("UPDATE newsroom_articles SET body_source = 'fetch' WHERE body_source = 'browser'")
    op.drop_constraint(op.f(CONSTRAINT), "newsroom_articles", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT),
        "newsroom_articles",
        "body_source IS NULL OR body_source IN ('feed','fetch','manual')",
    )
