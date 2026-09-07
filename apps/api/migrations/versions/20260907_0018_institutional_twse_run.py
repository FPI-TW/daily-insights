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
    # so the stored name is looked up instead of spelled out.
    names = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = CAST(:table AS regclass) AND contype = 'c' "
                # PostgreSQL stores `operation IN (...)` as `(operation)::text = ANY (...)`.
                "AND pg_get_constraintdef(oid) LIKE '%(operation)::text = ANY%'"
            ),
            {"table": TABLE},
        )
        .scalars()
        .all()
    )
    if len(names) != 1:
        raise RuntimeError(f"expected one operation check constraint, found {names}")
    # op.f() marks the looked-up name as final so the convention is not applied again.
    op.drop_constraint(op.f(names[0]), TABLE, type_="check")


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
