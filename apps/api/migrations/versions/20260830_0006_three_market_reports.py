"""Persist three-market manifest identity and nullable source dates.

Revision ID: 20260830_0006
Revises: 20260725_0005
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0006"
down_revision: str | None = "20260725_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_HASH = "0" * 64


def upgrade() -> None:
    for table in ("report_pipeline_runs", "source_runs", "report_publications"):
        op.add_column(
            table,
            sa.Column(
                "manifest_version",
                sa.String(length=100),
                server_default="legacy.v1",
                nullable=False,
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "manifest_hash",
                sa.String(length=64),
                server_default=_LEGACY_HASH,
                nullable=False,
            ),
        )
        op.create_check_constraint(
            op.f(f"ck_{table}_manifest_hash_sha256"),
            table,
            "char_length(manifest_hash) = 64",
        )
        op.alter_column(table, "manifest_version", server_default=None)
        op.alter_column(table, "manifest_hash", server_default=None)
    op.alter_column("report_publications", "source_as_of", existing_type=sa.Date(), nullable=True)


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM report_publications WHERE source_as_of IS NULL) THEN
                RAISE EXCEPTION 'cannot downgrade while unavailable report editions exist';
            END IF;
        END
        $$
        """
    )
    op.alter_column("report_publications", "source_as_of", existing_type=sa.Date(), nullable=False)
    for table in reversed(("report_pipeline_runs", "source_runs", "report_publications")):
        op.drop_constraint(op.f(f"ck_{table}_manifest_hash_sha256"), table, type_="check")
        op.drop_column(table, "manifest_hash")
        op.drop_column(table, "manifest_version")
