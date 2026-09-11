"""Release ^TWII from yfinance so TWSE can own the series.

Revision ID: 20260911_0024
Revises: 20260909_0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260911_0024"
down_revision: str | None = "20260909_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYMBOL = "^TWII"
OLD_PROVIDER = "yfinance"
NEW_PROVIDER = "twse"


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
    refresh does it instead, widening its window to two years on its own when
    it finds the series empty. Ownership is left alone; `store_index_daily_bars`
    reassigns it once no bars remain.

    Scoped to the old provider so it is idempotent and leaves an
    already-migrated deployment untouched.
    """
    op.execute(
        sa.text(
            "DELETE FROM index_daily_bars WHERE symbol = :symbol AND provider = :provider"
        ).bindparams(symbol=SYMBOL, provider=OLD_PROVIDER)
    )


def downgrade() -> None:
    """Release the series back, leaving it empty for a yfinance refresh.

    The yfinance bars this replaced cannot be reconstructed here for the same
    reason they could not be updated in place. Dropping the TWSE rows and the
    ownership claim returns ^TWII to the state a fresh yfinance refresh expects,
    which then backfills two years from Yahoo.
    """
    op.execute(
        sa.text(
            "DELETE FROM index_daily_bars WHERE symbol = :symbol AND provider = :provider"
        ).bindparams(symbol=SYMBOL, provider=NEW_PROVIDER)
    )
    op.execute(
        sa.text(
            "DELETE FROM index_daily_bar_series WHERE symbol = :symbol AND provider = :provider"
        ).bindparams(symbol=SYMBOL, provider=NEW_PROVIDER)
    )
