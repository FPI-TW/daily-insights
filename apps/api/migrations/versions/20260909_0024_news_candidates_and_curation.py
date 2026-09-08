"""Record news candidates, manual curation columns and the news publish queue.

Revision ID: 20260909_0024
Revises: 20260909_0023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260909_0024"
down_revision: str | None = "20260909_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS = "data_management_runs"
ITEMS = "news_items"
CANDIDATES = "news_candidates"
STATUS_VALID = "status IN ('pending', 'running', 'succeeded', 'partial', 'failed', 'cancelled')"
OLD_OPERATIONS = (
    "'morning_all', 'morning_market', 'index_yahoo', 'institutional_twse', 'news_all', "
    "'news_market', 'macro_dashboard'"
)
NEW_OPERATIONS = (
    "'morning_all', 'morning_market', 'index_yahoo', 'institutional_twse', 'news_all', "
    "'news_market', 'news_publish', 'macro_dashboard'"
)


def _scope_check(unscoped_operations: str) -> str:
    return (
        "((operation = 'morning_market' AND market_code IN "
        "('global_macro_bonds', 'crypto', 'us_equity')) OR "
        "(operation = 'news_market' AND market_code IN "
        "('global', 'tw_equity', 'us_equity')) OR "
        f"(operation IN ({unscoped_operations}) AND market_code IS NULL))"
    )


OLD_SCOPE = _scope_check(
    "'morning_all', 'index_yahoo', 'institutional_twse', 'news_all', 'macro_dashboard'"
)
NEW_SCOPE = _scope_check(
    "'morning_all', 'index_yahoo', 'institutional_twse', 'news_all', 'news_publish', "
    "'macro_dashboard'"
)


def _replace_validation_checks() -> None:
    """Drop the queue's three validation checks by catalog identity.

    Migration 0021 recreated them under stable names, but the physical name
    still depends on the naming convention in force at that time, so they are
    enumerated from the catalog and their cardinality validated before any is
    dropped, exactly as 0021 did.
    """
    op.execute(
        sa.text(
            "DO $$ DECLARE constraint_names text[]; constraint_name text; BEGIN "
            "SELECT array_agg(conname ORDER BY conname) INTO constraint_names "
            "FROM pg_constraint "
            f"WHERE conrelid = CAST('{RUNS}' AS regclass) AND contype = 'c'; "
            "IF coalesce(array_length(constraint_names, 1), 0) <> 3 THEN "
            "RAISE EXCEPTION USING MESSAGE = 'unexpected data-management check constraints: ' "
            "|| coalesce(array_to_string(constraint_names, ', '), '<none>'); END IF; "
            "FOREACH constraint_name IN ARRAY constraint_names LOOP "
            f"EXECUTE 'ALTER TABLE {RUNS} DROP CONSTRAINT ' || quote_ident(constraint_name); "
            "END LOOP; END $$"
        )
    )


def _create_validation_checks(operations: str, scope: str) -> None:
    op.create_check_constraint("operation_valid", RUNS, f"operation IN ({operations})")
    op.create_check_constraint("status_valid", RUNS, STATUS_VALID)
    op.create_check_constraint("market_code_valid_for_operation", RUNS, scope)


def upgrade() -> None:
    _replace_validation_checks()
    _create_validation_checks(NEW_OPERATIONS, NEW_SCOPE)
    op.add_column(
        RUNS, sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_index(
        "uq_data_management_runs_active_news_publish",
        RUNS,
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running') AND operation = 'news_publish'"),
    )

    op.add_column(
        ITEMS,
        sa.Column("origin", sa.String(length=10), nullable=False, server_default="model"),
    )
    op.add_column(ITEMS, sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        ITEMS, sa.Column("hidden_by_user_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        ITEMS, sa.Column("published_by_user_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.create_check_constraint("origin_valid", ITEMS, "origin IN ('model','manual')")
    op.create_foreign_key(
        "fk_news_items_hidden_by_user_id_users",
        ITEMS,
        "users",
        ["hidden_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_news_items_published_by_user_id_users",
        ITEMS,
        "users",
        ["published_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        CANDIDATES,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "edition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "news_editions.id",
                ondelete="RESTRICT",
                name="fk_news_candidates_edition_id_news_editions",
            ),
            nullable=False,
        ),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("source_name", sa.String(length=100), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("headline", sa.String(length=1_000), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_digest", sa.String(length=64), nullable=True),
        sa.Column("stage", sa.String(length=20), nullable=False),
        sa.Column("drop_reason", sa.String(length=30), nullable=True),
        sa.Column("ai_rank", sa.Integer(), nullable=True),
        sa.Column("ai_topic", sa.String(length=50), nullable=True),
        sa.Column("ai_market", sa.String(length=20), nullable=True),
        sa.Column("ai_importance", sa.Integer(), nullable=True),
        sa.Column("ai_event_key", sa.String(length=80), nullable=True),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "news_items.id",
                ondelete="SET NULL",
                name="fk_news_candidates_item_id_news_items",
            ),
            nullable=True,
        ),
        sa.Column("publish_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("publish_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "publish_requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id",
                ondelete="RESTRICT",
                name="fk_news_candidates_publish_requested_by_user_id_users",
            ),
            nullable=True,
        ),
        sa.Column("publish_error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "stage IN ('discovered','fetch_failed','unused','reviewed','dropped','published')",
            name="stage_valid",
        ),
        sa.CheckConstraint(
            "drop_reason IS NULL OR drop_reason IN "
            "('off_market','policy','duplicate_event','summary_failed','reserve')",
            name="drop_reason_valid",
        ),
        sa.UniqueConstraint("edition_id", "candidate_id", name="uq_news_candidate_edition"),
    )
    op.create_index("ix_news_candidates_edition_id", CANDIDATES, ["edition_id"])


def downgrade() -> None:
    # Manual publish runs and candidate records cannot be represented by the
    # previous schema. Refuse before touching any schema object rather than
    # deleting curation history during a rollback.
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            f"IF EXISTS (SELECT 1 FROM {RUNS} WHERE operation = 'news_publish') "
            f"OR EXISTS (SELECT 1 FROM {CANDIDATES}) THEN "
            "RAISE EXCEPTION 'cannot downgrade news schema while "
            "publish runs or candidate records exist'; "
            "END IF; END $$"
        )
    )
    op.drop_index("ix_news_candidates_edition_id", table_name=CANDIDATES)
    op.drop_table(CANDIDATES)
    op.drop_constraint("fk_news_items_published_by_user_id_users", ITEMS, type_="foreignkey")
    op.drop_constraint("fk_news_items_hidden_by_user_id_users", ITEMS, type_="foreignkey")
    op.drop_constraint("origin_valid", ITEMS, type_="check")
    op.drop_column(ITEMS, "published_by_user_id")
    op.drop_column(ITEMS, "hidden_by_user_id")
    op.drop_column(ITEMS, "hidden_at")
    op.drop_column(ITEMS, "origin")
    op.drop_index("uq_data_management_runs_active_news_publish", table_name=RUNS)
    op.drop_column(RUNS, "payload")
    _replace_validation_checks()
    _create_validation_checks(OLD_OPERATIONS, OLD_SCOPE)
