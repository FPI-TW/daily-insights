"""Add durable daily-news data-management operations.

Revision ID: 20260907_0020
Revises: 20260907_0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260907_0020"
down_revision: str | None = "20260907_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "data_management_runs"
OLD_OPERATIONS = "'morning_all', 'morning_market', 'index_yahoo', 'institutional_twse'"
NEW_OPERATIONS = f"{OLD_OPERATIONS}, 'news_all', 'news_market'"
OLD_SCOPE = "(operation = 'morning_market') = (market_code IS NOT NULL)"
NEW_SCOPE = (
    "((operation = 'morning_market' AND market_code IN "
    "('global_macro_bonds', 'crypto', 'us_equity')) OR "
    "(operation = 'news_market' AND market_code IN "
    "('global', 'tw_equity', 'us_equity')) OR "
    "(operation IN ('morning_all', 'index_yahoo', 'institutional_twse', 'news_all') "
    "AND market_code IS NULL))"
)


def _drop_check(*, match: str, description: str) -> None:
    """Drop the one check whose PostgreSQL definition contains ``match``.

    The original 0016 migration supplied already-prefixed constraint names to
    a naming convention, so PostgreSQL may store a truncated generated name.
    Resolve it by definition instead of relying on that physical name.
    """
    op.execute(
        sa.text(
            "DO $$\n"
            "DECLARE\n"
            "    names text[];\n"
            "BEGIN\n"
            "    SELECT array_agg(conname) INTO names\n"
            "    FROM pg_constraint\n"
            f"    WHERE conrelid = CAST('{TABLE}' AS regclass)\n"
            "      AND contype = 'c'\n"
            f"      AND position('{match}' IN pg_get_constraintdef(oid)) > 0;\n"
            "    IF coalesce(array_length(names, 1), 0) <> 1 THEN\n"
            "        RAISE EXCEPTION USING MESSAGE = "
            f"'expected one {description} check constraint, found: ' "
            "|| coalesce(array_to_string(names, ', '), '<none>');\n"
            "    END IF;\n"
            f"    EXECUTE 'ALTER TABLE {TABLE} DROP CONSTRAINT ' || quote_ident(names[1]);\n"
            "END $$"
        )
    )


def _drop_operation_check() -> None:
    _drop_check(match="(operation)::text = ANY", description="operation")


def _drop_market_scope_check() -> None:
    _drop_check(match="market_code", description="market scope")


def upgrade() -> None:
    op.drop_constraint("fk_data_management_runs_market_code_markets", TABLE, type_="foreignkey")
    _drop_operation_check()
    _drop_market_scope_check()
    op.create_check_constraint("operation_valid", TABLE, f"operation IN ({NEW_OPERATIONS})")
    op.create_check_constraint("market_code_valid_for_operation", TABLE, NEW_SCOPE)
    op.create_index(
        "uq_data_management_runs_active_news",
        TABLE,
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'running') AND operation IN ('news_all', 'news_market')"
        ),
    )


def downgrade() -> None:
    # News runs are durable queue/history records and cannot be represented by
    # the prior schema. Refuse before changing any schema object.
    op.execute(
        sa.text(
            "DO $$\n"
            "BEGIN\n"
            "    IF EXISTS (\n"
            f"        SELECT 1 FROM {TABLE}\n"
            "        WHERE operation IN ('news_all', 'news_market')\n"
            "    ) THEN\n"
            "        RAISE EXCEPTION "
            "'cannot downgrade data-management schema while news run history exists';\n"
            "    END IF;\n"
            "END $$"
        )
    )
    op.drop_index("uq_data_management_runs_active_news", table_name=TABLE)
    _drop_market_scope_check()
    _drop_operation_check()
    op.create_check_constraint("operation_valid", TABLE, f"operation IN ({OLD_OPERATIONS})")
    op.create_check_constraint("market_scope_matches_operation", TABLE, OLD_SCOPE)
    op.create_foreign_key(
        "fk_data_management_runs_market_code_markets",
        TABLE,
        "markets",
        ["market_code"],
        ["code"],
        ondelete="RESTRICT",
    )
