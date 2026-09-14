"""Add durable news checkpoints, shared dependency health and recovery identity.

Revision ID: 20260911_0025
Revises: 20260909_0024
Create Date: 2026-09-11 14:15:54.359484
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260911_0025"
down_revision: str | None = "20260909_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_dependency_states",
        sa.Column("scope", sa.String(length=300), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("failure", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_run_id", sa.UUID(), nullable=True),
        sa.Column("failures_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("newest_article_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("scope", name=op.f("pk_news_dependency_states")),
    )
    op.create_table(
        "news_workflows",
        sa.Column("root_run_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("market_code", sa.String(length=50), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("stage", sa.String(length=30), nullable=False),
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("failures", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_workflows")),
        sa.UniqueConstraint("root_run_id", "market_code", name="uq_news_workflow_root_market"),
    )
    op.create_index(
        "ix_news_workflows_market_date",
        "news_workflows",
        ["market_code", "edition_date"],
        unique=False,
    )
    op.create_table(
        "news_checkpoints",
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=30), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("repairs", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["news_workflows.id"],
            name=op.f("fk_news_checkpoints_workflow_id_news_workflows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_checkpoints")),
        sa.UniqueConstraint("workflow_id", "key", name="uq_news_checkpoint_workflow_key"),
    )
    op.create_index(
        "ix_news_checkpoints_expires_at", "news_checkpoints", ["expires_at"], unique=False
    )
    op.add_column(
        "data_management_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("data_management_runs", sa.Column("resume_of_id", sa.UUID(), nullable=True))
    op.create_index(
        "uq_data_management_runs_resume_of", "data_management_runs", ["resume_of_id"], unique=True
    )


def downgrade() -> None:
    # Never silently discard recovery history or unblock a paused provider.
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM news_workflows) "
            "OR EXISTS (SELECT 1 FROM news_dependency_states) "
            "OR EXISTS (SELECT 1 FROM data_management_runs WHERE resume_of_id IS NOT NULL) "
            "THEN RAISE EXCEPTION 'cannot downgrade while news recovery history exists'; "
            "END IF; END $$"
        )
    )
    op.drop_index("uq_data_management_runs_resume_of", table_name="data_management_runs")
    op.drop_column("data_management_runs", "resume_of_id")
    op.drop_column("data_management_runs", "heartbeat_at")
    op.drop_index("ix_news_checkpoints_expires_at", table_name="news_checkpoints")
    op.drop_table("news_checkpoints")
    op.drop_index("ix_news_workflows_market_date", table_name="news_workflows")
    op.drop_table("news_workflows")
    op.drop_table("news_dependency_states")
