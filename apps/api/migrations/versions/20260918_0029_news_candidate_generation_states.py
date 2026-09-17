"""Classify prepared candidates and translation failures.

Revision ID: 20260918_0029
Revises: 20260916_0028
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260918_0029"
down_revision: str | None = "20260916_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "news_candidates"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_news_candidates_stage_valid"), TABLE, type_="check")
    op.drop_constraint(op.f("ck_news_candidates_drop_reason_valid"), TABLE, type_="check")
    op.create_check_constraint(
        op.f("ck_news_candidates_stage_valid"),
        TABLE,
        "stage IN "
        "('discovered','fetch_failed','unused','reviewed','prepared','dropped','published')",
    )
    op.create_check_constraint(
        op.f("ck_news_candidates_drop_reason_valid"),
        TABLE,
        "drop_reason IS NULL OR drop_reason IN "
        "('off_market','policy','duplicate_event','summary_failed','translation_failed','reserve')",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM news_candidates "
            "WHERE stage = 'prepared' OR drop_reason = 'translation_failed') THEN "
            "RAISE EXCEPTION 'cannot downgrade candidate states while new values exist'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint(op.f("ck_news_candidates_stage_valid"), TABLE, type_="check")
    op.drop_constraint(op.f("ck_news_candidates_drop_reason_valid"), TABLE, type_="check")
    op.create_check_constraint(
        op.f("ck_news_candidates_stage_valid"),
        TABLE,
        "stage IN ('discovered','fetch_failed','unused','reviewed','dropped','published')",
    )
    op.create_check_constraint(
        op.f("ck_news_candidates_drop_reason_valid"),
        TABLE,
        "drop_reason IS NULL OR drop_reason IN "
        "('off_market','policy','duplicate_event','summary_failed','reserve')",
    )
