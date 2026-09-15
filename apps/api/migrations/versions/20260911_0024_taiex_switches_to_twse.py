"""Release ^TWII from yfinance so TWSE can own the series.

Revision ID: 20260911_0024
Revises: 20260911_0025

Re-pointed after main gained the news-recovery migrations: the revision id is
left alone because environments already stamped with it would otherwise lose
their place in the graph. Alembic orders by the graph, not by the filename.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260911_0024"
down_revision: str | None = "20260911_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYMBOL = "^TWII"
OLD_PROVIDER = "yfinance"


def upgrade() -> None:
    """Delete ^TWII's yfinance bars. The next refresh rebuilds them from TWSE.

    ^TWII now comes from the exchange rather than Yahoo, whose volume for this
    series disagreed with the published share count by three orders of
    magnitude and whose closes carried float error. `index_daily_bar_series`
    allows one provider per symbol, so the old rows have to go before TWSE can
    claim it -- and they cannot be updated in place, because volume only exists
    upstream and would have to be refetched anyway.

    Nothing is backfilled here: a migration must not depend on an external API
    being reachable, and these two TWSE reports take minutes to walk. The
    refresh does it instead, continually selecting missing months until the
    required 25-month window is complete. Ownership is left alone;
    `store_index_daily_bars` reassigns it once no bars remain.

    Scoped to the old provider so it is idempotent and leaves an
    already-migrated deployment untouched.
    """
    op.execute(
        sa.text(
            "DELETE FROM index_daily_bars WHERE symbol = :symbol AND provider = :provider"
        ).bindparams(symbol=SYMBOL, provider=OLD_PROVIDER)
    )


def downgrade() -> None:
    """Refuse an unsafe rollback to the known-bad Yahoo source.

    The upgrade deletes erroneous Yahoo history, while the replacement TWSE
    history may already be serving the product. Neither deleting that valid
    history nor repopulating from Yahoo is a safe automatic downgrade. A
    rollback therefore requires an explicit coordinated data/provider plan.
    """
    raise RuntimeError(
        "20260911_0024 is irreversible: retain TWSE ^TWII history and perform "
        "a coordinated application/data rollback"
    )
