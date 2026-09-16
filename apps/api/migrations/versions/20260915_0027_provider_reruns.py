"""Add provider-scoped reruns.

Revision ID: 20260915_0027
Revises: 20260915_0026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0027"
down_revision: str | None = "20260915_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS = "data_management_runs"
STATUS = "status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'cancelled')"
OPERATIONS = (
    "'morning_all', 'morning_market', 'index_yahoo', 'institutional_twse', "
    "'news_all', 'news_market', 'news_publish', 'macro_dashboard', 'provider_rerun'"
)
SCOPE = (
    "((operation = 'morning_market' AND market_code IN "
    "('global_macro_bonds', 'crypto', 'us_equity')) OR "
    "(operation = 'provider_rerun' AND market_code IN "
    "('twelve_data', 'yahoo_finance', 'twse')) OR "
    "(operation = 'news_market' AND market_code IN "
    "('global', 'tw_equity', 'us_equity')) OR "
    "(operation IN ('morning_all', 'index_yahoo', 'institutional_twse', 'news_all', "
    "'news_publish', 'macro_dashboard') AND market_code IS NULL))"
)


def _replace_checks() -> None:
    op.execute(
        sa.text(
            "DO $$ DECLARE constraint_names text[]; constraint_name text; BEGIN "
            "SELECT array_agg(conname ORDER BY conname) INTO constraint_names "
            f"FROM pg_constraint WHERE conrelid = CAST('{RUNS}' AS regclass) AND contype = 'c'; "
            "IF coalesce(array_length(constraint_names, 1), 0) <> 3 THEN "
            "RAISE EXCEPTION USING MESSAGE = 'unexpected data-management check constraints: ' "
            "|| coalesce(array_to_string(constraint_names, ', '), '<none>'); END IF; "
            "FOREACH constraint_name IN ARRAY constraint_names LOOP "
            f"EXECUTE 'ALTER TABLE {RUNS} DROP CONSTRAINT ' || quote_ident(constraint_name); "
            "END LOOP; END $$"
        )
    )


def upgrade() -> None:
    _replace_checks()
    op.create_check_constraint("operation_valid", RUNS, f"operation IN ({OPERATIONS})")
    op.create_check_constraint("status_valid", RUNS, STATUS)
    op.create_check_constraint("market_code_valid_for_operation", RUNS, SCOPE)
    op.create_index(
        "uq_data_management_runs_active_provider_rerun",
        RUNS,
        ["market_code"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'running') AND operation = 'provider_rerun'"
        ),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {RUNS} WHERE operation = 'provider_rerun') THEN "
            "RAISE EXCEPTION 'cannot downgrade while provider rerun history exists'; "
            "END IF; END $$"
        )
    )
    op.drop_index("uq_data_management_runs_active_provider_rerun", table_name=RUNS)
    _replace_checks()
    op.create_check_constraint(
        "operation_valid",
        RUNS,
        "operation IN ('morning_all', 'morning_market', 'index_yahoo', 'institutional_twse', "
        "'news_all', 'news_market', 'news_publish', 'macro_dashboard')",
    )
    op.create_check_constraint("status_valid", RUNS, STATUS)
    op.create_check_constraint(
        "market_code_valid_for_operation",
        RUNS,
        "((operation = 'morning_market' AND market_code IN "
        "('global_macro_bonds', 'crypto', 'us_equity')) OR "
        "(operation = 'news_market' AND market_code IN "
        "('global', 'tw_equity', 'us_equity')) OR "
        "(operation IN ('morning_all', 'index_yahoo', 'institutional_twse', 'news_all', "
        "'news_publish', 'macro_dashboard') AND market_code IS NULL))",
    )
