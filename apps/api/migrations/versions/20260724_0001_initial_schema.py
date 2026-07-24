"""Create the fresh-start modular-monolith schema.

Revision ID: 20260724_0001
Revises:
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from daily_insights_api.modules.markets.catalog import MARKETS

revision: str = "20260724_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

organization_status = postgresql.ENUM(
    "active", "suspended", "archived", name="organization_status", create_type=False
)
system_role = postgresql.ENUM(
    "admin", "asset_manager", "org_member", name="system_role", create_type=False
)
user_status = postgresql.ENUM(
    "invited", "active", "suspended", name="user_status", create_type=False
)
message_role = postgresql.ENUM(
    "user", "assistant", "system", name="message_role", create_type=False
)
generation_status = postgresql.ENUM(
    "pending", "complete", "partial", "error", name="generation_status", create_type=False
)
asset_kind = postgresql.ENUM(
    "audio", "image", "downloadable", "pdf", name="asset_kind", create_type=False
)
asset_status = postgresql.ENUM(
    "active", "archived", "deleted", name="asset_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in (
        organization_status,
        system_role,
        user_status,
        message_role,
        generation_status,
        asset_kind,
        asset_status,
    ):
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "organizations",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("seat_limit", sa.Integer(), nullable=True),
        sa.Column("status", organization_status, server_default="active", nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "seat_limit IS NULL OR seat_limit > 0",
            name=op.f("ck_organizations_seat_limit_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("slug", name=op.f("uq_organizations_slug")),
    )
    op.create_index(op.f("ix_organizations_status"), "organizations", ["status"], unique=False)

    op.create_table(
        "users",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("system_role", system_role, server_default="org_member", nullable=False),
        sa.Column("status", user_status, server_default="invited", nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "email = lower(btrim(email))",
            name=op.f("ck_users_email_normalized"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_index(op.f("ix_users_status"), "users", ["status"], unique=False)
    op.create_index(op.f("ix_users_system_role"), "users", ["system_role"], unique=False)

    op.create_table(
        "markets",
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name_en", sa.String(length=100), nullable=False),
        sa.Column("name_zh_hant", sa.String(length=100), nullable=False),
        sa.Column("name_zh_hans", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_markets")),
    )
    markets_table = sa.table(
        "markets",
        sa.column("code", sa.String(length=50)),
        sa.column("name_en", sa.String(length=100)),
        sa.column("name_zh_hant", sa.String(length=100)),
        sa.column("name_zh_hans", sa.String(length=100)),
    )
    op.bulk_insert(
        markets_table,
        [
            {
                "code": market.code,
                "name_en": market.name_en,
                "name_zh_hant": market.name_zh_hant,
                "name_zh_hans": market.name_zh_hans,
            }
            for market in MARKETS
        ],
    )

    op.create_table(
        "memberships",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_memberships_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint(
            "organization_id", "user_id", name=op.f("uq_memberships_organization_id")
        ),
    )
    op.create_index(
        op.f("ix_memberships_organization_id"),
        "memberships",
        ["organization_id"],
        unique=False,
    )
    op.create_index(op.f("ix_memberships_user_id"), "memberships", ["user_id"], unique=False)

    op.create_table(
        "organization_market_policies",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("market_code", sa.String(length=50), nullable=False),
        sa.Column("is_visible", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("contract_reference", sa.String(length=200), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("changed_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_user_id"],
            ["users.id"],
            name=op.f("fk_organization_market_policies_changed_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["market_code"],
            ["markets.code"],
            name=op.f("fk_organization_market_policies_market_code_markets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_market_policies_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_market_policies")),
        sa.UniqueConstraint(
            "organization_id",
            "market_code",
            name=op.f("uq_organization_market_policies_organization_id"),
        ),
    )
    op.create_index(
        op.f("ix_organization_market_policies_market_code"),
        "organization_market_policies",
        ["market_code"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_market_policies_organization_id"),
        "organization_market_policies",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "conversations",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["memberships.organization_id", "memberships.user_id"],
            name="fk_conversations_membership",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        "ix_conversations_org_created",
        "conversations",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_conversations_user_created",
        "conversations",
        ["user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "messages",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("role", message_role, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", generation_status, server_default="complete", nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence_number >= 0",
            name=op.f("ck_messages_sequence_number_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "conversation_id",
            "sequence_number",
            name=op.f("uq_messages_conversation_id"),
        ),
    )
    op.create_index(
        "ix_messages_conversation_created",
        "messages",
        ["conversation_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "model_configurations",
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("requested_model", sa.String(length=200), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_model_configurations_version_positive")),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_model_configurations_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_configurations")),
        sa.UniqueConstraint(
            "id",
            "version",
            name="uq_model_configurations_id_version",
        ),
        sa.UniqueConstraint("version", name=op.f("uq_model_configurations_version")),
    )
    op.create_index(
        "uq_model_configurations_single_active",
        "model_configurations",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "generation_records",
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_configuration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_configuration_version", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("requested_model", sa.String(length=200), nullable=False),
        sa.Column("resolved_model", sa.String(length=200), nullable=True),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("context_version", sa.String(length=100), nullable=True),
        sa.Column("report_version", sa.String(length=100), nullable=True),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", generation_status, server_default="pending", nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name=op.f("ck_generation_records_input_tokens_nonnegative"),
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name=op.f("ck_generation_records_latency_nonnegative"),
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name=op.f("ck_generation_records_output_tokens_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_generation_records_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_configuration_id", "model_configuration_version"],
            ["model_configurations.id", "model_configurations.version"],
            name="fk_generation_records_model_configuration_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_records")),
        sa.UniqueConstraint("message_id", name=op.f("uq_generation_records_message_id")),
    )
    op.create_index(
        op.f("ix_generation_records_provider_request_id"),
        "generation_records",
        ["provider_request_id"],
        unique=False,
    )
    op.create_index(
        "ix_generation_records_status_created",
        "generation_records",
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        "assets",
        sa.Column("bucket", sa.String(length=100), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("kind", asset_kind, nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("locale", sa.String(length=10), nullable=True),
        sa.Column("localized_titles", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", asset_status, server_default="active", nullable=False),
        sa.Column("uploaded_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_assets_size_bytes_nonnegative")),
        sa.ForeignKeyConstraint(
            ["deleted_by_user_id"],
            ["users.id"],
            name=op.f("fk_assets_deleted_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            name=op.f("fk_assets_uploaded_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assets")),
        sa.UniqueConstraint("object_key", name=op.f("uq_assets_object_key")),
    )
    op.create_index("ix_assets_status_kind", "assets", ["status", "kind"], unique=False)


def downgrade() -> None:
    op.drop_table("assets")
    op.drop_table("generation_records")
    op.drop_index("uq_model_configurations_single_active", table_name="model_configurations")
    op.drop_table("model_configurations")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("organization_market_policies")
    op.drop_table("memberships")
    op.drop_table("markets")
    op.drop_table("users")
    op.drop_table("organizations")

    bind = op.get_bind()
    for enum_type in (
        asset_status,
        asset_kind,
        generation_status,
        message_role,
        user_status,
        system_role,
        organization_status,
    ):
        enum_type.drop(bind, checkfirst=True)
