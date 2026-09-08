"""Persist global macro snapshots and add cancellable macro queue work.

Revision ID: 20260908_0021
Revises: 20260907_0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260908_0021"
down_revision: str | None = "20260907_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "data_management_runs"


def _replace_validation_checks(*, expected_counts: tuple[int, ...]) -> None:
    """Drop the predecessor's queue validation checks by catalog identity.

    The preceding migrations created unstable physical names via the naming
    convention, and the 0020 market-scope constraint is absent on databases
    upgraded from the shipped chain.  This table owns only these validation
    checks, so enumerate all of them and validate the expected cardinality
    rather than guessing from rendered PostgreSQL expressions.
    """
    expected = ", ".join(str(count) for count in expected_counts)
    op.execute(
        sa.text(
            "DO $$ DECLARE constraint_names text[]; constraint_name text; BEGIN "
            "SELECT array_agg(conname ORDER BY conname) INTO constraint_names "
            "FROM pg_constraint "
            f"WHERE conrelid = CAST('{TABLE}' AS regclass) AND contype = 'c'; "
            "IF coalesce(array_length(constraint_names, 1), 0) "
            f"NOT IN ({expected}) THEN "
            "RAISE EXCEPTION USING MESSAGE = 'unexpected data-management check constraints: ' "
            "|| coalesce(array_to_string(constraint_names, ', '), '<none>'); END IF; "
            "FOREACH constraint_name IN ARRAY constraint_names LOOP "
            f"EXECUTE 'ALTER TABLE {TABLE} DROP CONSTRAINT ' || quote_ident(constraint_name); "
            "END LOOP; END $$"
        )
    )


def upgrade() -> None:
    _replace_validation_checks(expected_counts=(2, 3))
    op.create_check_constraint(
        "operation_valid",
        TABLE,
        "operation IN ('morning_all', 'morning_market', 'index_yahoo', "
        "'institutional_twse', 'news_all', 'news_market', 'macro_dashboard')",
    )
    op.create_check_constraint(
        "status_valid",
        TABLE,
        "status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'cancelled')",
    )
    op.create_check_constraint(
        "market_code_valid_for_operation",
        TABLE,
        "((operation = 'morning_market' AND market_code IN "
        "('global_macro_bonds', 'crypto', 'us_equity')) OR "
        "(operation = 'news_market' AND market_code IN "
        "('global', 'tw_equity', 'us_equity')) OR "
        "(operation IN ('morning_all', 'index_yahoo', 'institutional_twse', 'news_all', "
        "'macro_dashboard') AND market_code IS NULL))",
    )
    op.create_index(
        "uq_data_management_runs_active_manual_macro_dashboard",
        TABLE,
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'running') AND operation = 'macro_dashboard' "
            "AND requested_by_user_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_data_management_runs_running_macro_dashboard",
        TABLE,
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text("status = 'running' AND operation = 'macro_dashboard'"),
    )
    op.create_index(
        "uq_data_management_runs_automatic_macro_dashboard_edition",
        TABLE,
        ["edition_date"],
        unique=True,
        postgresql_where=sa.text("operation = 'macro_dashboard' AND requested_by_user_id IS NULL"),
    )
    op.create_table(
        "macro_dashboard_snapshots",
        sa.Column("scope_key", sa.String(length=64), primary_key=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    # Neither macro work nor a cancellation terminal state can be represented
    # by the previous queue contract. Preserve durable history rather than
    # silently deleting or rewriting it during a rollback.
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {TABLE} "
            "WHERE operation = 'macro_dashboard' OR status = 'cancelled') "
            "OR EXISTS (SELECT 1 FROM macro_dashboard_snapshots) THEN "
            "RAISE EXCEPTION 'cannot downgrade data-management schema while "
            "macro, cancelled, or snapshot history exists'; "
            "END IF; END $$"
        )
    )
    op.drop_table("macro_dashboard_snapshots")
    op.drop_index("uq_data_management_runs_automatic_macro_dashboard_edition", table_name=TABLE)
    op.drop_index("uq_data_management_runs_running_macro_dashboard", table_name=TABLE)
    op.drop_index("uq_data_management_runs_active_manual_macro_dashboard", table_name=TABLE)
    _replace_validation_checks(expected_counts=(3,))
    op.create_check_constraint(
        "operation_valid",
        TABLE,
        "operation IN ('morning_all', 'morning_market', 'index_yahoo', "
        "'institutional_twse', 'news_all', 'news_market')",
    )
    op.create_check_constraint(
        "status_valid",
        TABLE,
        "status IN ('pending', 'running', 'succeeded', 'partial', 'failed')",
    )
    op.create_check_constraint(
        "market_code_valid_for_operation",
        TABLE,
        "((operation = 'morning_market' AND market_code IN "
        "('global_macro_bonds', 'crypto', 'us_equity')) OR "
        "(operation = 'news_market' AND market_code IN "
        "('global', 'tw_equity', 'us_equity')) OR "
        "(operation IN ('morning_all', 'index_yahoo', 'institutional_twse', 'news_all') "
        "AND market_code IS NULL))",
    )
