"""Add retained, page-contextual streaming chat state.

Revision ID: 20260902_0008
Revises: 20260901_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260902_0008"
down_revision: str | None = "20260901_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("client_request_id", postgresql.UUID(as_uuid=True)))
    op.add_column("messages", sa.Column("reply_to_message_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        op.f("fk_messages_reply_to_message_id_messages"),
        "messages",
        "messages",
        ["reply_to_message_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint("uq_messages_client_request_id", "messages", ["client_request_id"])
    op.create_index(
        "uq_messages_pending_assistant_conversation",
        "messages",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("role = 'assistant' AND status = 'pending'"),
    )
    op.add_column(
        "generation_records",
        sa.Column(
            "context_snapshot",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("generation_records", sa.Column("context_digest", sa.String(length=64)))
    op.add_column(
        "generation_records",
        sa.Column("context_truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.alter_column("generation_records", "context_snapshot", server_default=None)
    op.alter_column("generation_records", "context_truncated", server_default=None)
    op.alter_column("model_configurations", "created_by_user_id", nullable=True)
    op.alter_column("active_model_configuration", "activated_by_user_id", nullable=True)


def downgrade() -> None:
    # Do not restore NOT NULL here. Environment-driven model synchronization
    # deliberately creates actor-less immutable history; coercing it to a
    # fabricated user would corrupt audit provenance and making it NOT NULL
    # would make a real downgrade fail. Older application code tolerates the
    # nullable FK, so retaining this safe superset is the reversible path.
    op.drop_column("generation_records", "context_truncated")
    op.drop_column("generation_records", "context_digest")
    op.drop_column("generation_records", "context_snapshot")
    op.drop_index("uq_messages_pending_assistant_conversation", table_name="messages")
    op.drop_constraint("uq_messages_client_request_id", "messages", type_="unique")
    op.drop_constraint(
        op.f("fk_messages_reply_to_message_id_messages"), "messages", type_="foreignkey"
    )
    op.drop_column("messages", "reply_to_message_id")
    op.drop_column("messages", "client_request_id")
