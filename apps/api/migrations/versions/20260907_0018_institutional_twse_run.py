"""Allow the institutional_twse data-management operation.

Revision ID: 20260907_0018
Revises: 20260907_0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260907_0018"
down_revision: str | None = "20260907_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "data_management_runs"
OLD_OPERATIONS = "'morning_all', 'morning_market', 'index_yahoo'"
NEW_OPERATIONS = f"{OLD_OPERATIONS}, 'institutional_twse'"


def _drop_operation_check() -> None:
    # 0016 named this constraint with the `ck_` prefix already applied, and the
    # metadata naming convention prefixed it again and truncated it with a hash,
    # so the stored name has to be looked up rather than spelled out. The lookup
    # runs inside the emitted SQL because `op.get_bind().execute()` returns None
    # under `alembic upgrade --sql`, which the README documents as offline mode.
    # No `%` anywhere in the statement: it reaches the driver as a parameterless
    # query, and psycopg would read one as a placeholder.
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
            # PostgreSQL stores `operation IN (...)` as `(operation)::text = ANY (...)`.
            "      AND position('(operation)::text = ANY' "
            "IN pg_get_constraintdef(oid)) > 0;\n"
            "    IF coalesce(array_length(names, 1), 0) <> 1 THEN\n"
            "        RAISE EXCEPTION USING MESSAGE = "
            "'expected one operation check constraint, found: ' "
            "|| coalesce(array_to_string(names, ', '), '<none>');\n"
            "    END IF;\n"
            f"    EXECUTE 'ALTER TABLE {TABLE} DROP CONSTRAINT ' || quote_ident(names[1]);\n"
            "END $$"
        )
    )


def upgrade() -> None:
    _drop_operation_check()
    op.create_check_constraint("operation_valid", TABLE, f"operation IN ({NEW_OPERATIONS})")
    op.create_index(
        "uq_data_management_runs_active_institutional",
        TABLE,
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'running') AND operation = 'institutional_twse'"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_data_management_runs_active_institutional", table_name=TABLE)
    op.execute(sa.text(f"DELETE FROM {TABLE} WHERE operation = 'institutional_twse'"))
    _drop_operation_check()
    op.create_check_constraint("operation_valid", TABLE, f"operation IN ({OLD_OPERATIONS})")
