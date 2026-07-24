"""Add Phase 2B Podcast, verified assets, and retained history.

Revision ID: 20260724_0004
Revises: 20260724_0003
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260724_0004"
down_revision: str | None = "20260724_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_asset_status(values: tuple[str, ...], *, replacement: str) -> None:
    op.execute(f"ALTER TYPE asset_status RENAME TO {replacement}")
    quoted = ", ".join(f"'{value}'" for value in values)
    op.execute(f"CREATE TYPE asset_status AS ENUM ({quoted})")
    op.execute("ALTER TABLE assets ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE assets ALTER COLUMN status TYPE asset_status USING status::text::asset_status"
    )
    op.execute("ALTER TABLE assets ALTER COLUMN status SET DEFAULT 'active'::asset_status")
    op.execute(f"DROP TYPE {replacement}")


def upgrade() -> None:
    _replace_asset_status(
        (
            "pending_verification",
            "active",
            "quarantined",
            "missing",
            "archived",
            "deleted",
        ),
        replacement="asset_status_phase1",
    )
    op.create_check_constraint(
        op.f("ck_assets_active_metadata_complete"),
        "assets",
        "status != 'active' OR "
        "(size_bytes > 0 AND sha256 IS NOT NULL AND char_length(sha256) = 64)",
    )
    op.drop_index(
        "uq_model_configurations_single_active",
        table_name="model_configurations",
    )
    op.drop_column("model_configurations", "is_active")

    op.drop_constraint(
        op.f("fk_messages_conversation_id_conversations"),
        "messages",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_messages_conversation_id_conversations"),
        "messages",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        op.f("fk_generation_records_message_id_messages"),
        "generation_records",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_generation_records_message_id_messages"),
        "generation_records",
        "messages",
        ["message_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "active_model_configuration",
        sa.Column("singleton_id", sa.SmallInteger(), nullable=False),
        sa.Column("model_configuration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("activated_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "singleton_id = 1",
            name=op.f("ck_active_model_configuration_singleton_id_one"),
        ),
        sa.ForeignKeyConstraint(
            ["activated_by_user_id"],
            ["users.id"],
            name=op.f("fk_active_model_configuration_activated_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_configuration_id"],
            ["model_configurations.id"],
            name=op.f("fk_active_model_configuration_model_configuration_id_model_configurations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "singleton_id",
            name=op.f("pk_active_model_configuration"),
        ),
        sa.UniqueConstraint(
            "model_configuration_id",
            name=op.f("uq_active_model_configuration_model_configuration_id"),
        ),
    )

    op.create_table(
        "asset_migration_manifests",
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="planned", nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cutover_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=True),
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
            "char_length(idempotency_key) = 64",
            name=op.f("ck_asset_migration_manifests_idempotency_key_sha256"),
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'copying', 'verified', 'cutover', 'failed')",
            name=op.f("ck_asset_migration_manifests_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_asset_migration_manifests_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cutover_by_user_id"],
            ["users.id"],
            name=op.f("fk_asset_migration_manifests_cutover_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_migration_manifests")),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_asset_migration_manifests_idempotency_key"),
        ),
    )

    op.create_table(
        "asset_migration_entries",
        sa.Column("manifest_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("source_bucket", sa.String(length=100), nullable=False),
        sa.Column("source_key", sa.String(length=1024), nullable=False),
        sa.Column("target_bucket", sa.String(length=100), nullable=False),
        sa.Column("target_key", sa.String(length=1024), nullable=False),
        sa.Column("source_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("target_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("source_mime_type", sa.String(length=255), nullable=True),
        sa.Column("target_mime_type", sa.String(length=255), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column("target_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="planned", nullable=False),
        sa.Column("copied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_confirmed_at", sa.DateTime(timezone=True), nullable=True),
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
            "locale IN ('zh-TW', 'zh-CN', 'en')",
            name=op.f("ck_asset_migration_entries_locale_supported"),
        ),
        sa.CheckConstraint(
            "source_size_bytes IS NULL OR source_size_bytes > 0",
            name=op.f("ck_asset_migration_entries_source_size_positive"),
        ),
        sa.CheckConstraint(
            "source_sha256 IS NULL OR char_length(source_sha256) = 64",
            name=op.f("ck_asset_migration_entries_source_sha256_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'copied', 'verified', 'cutover', 'failed')",
            name=op.f("ck_asset_migration_entries_status_valid"),
        ),
        sa.CheckConstraint(
            "target_size_bytes IS NULL OR target_size_bytes > 0",
            name=op.f("ck_asset_migration_entries_target_size_positive"),
        ),
        sa.CheckConstraint(
            "target_sha256 IS NULL OR char_length(target_sha256) = 64",
            name=op.f("ck_asset_migration_entries_target_sha256_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"],
            ["asset_migration_manifests.id"],
            name=op.f("fk_asset_migration_entries_manifest_id_asset_migration_manifests"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_migration_entries")),
        sa.UniqueConstraint(
            "manifest_id",
            "source_bucket",
            "source_key",
            name=op.f("uq_asset_migration_entries_manifest_id"),
        ),
        sa.UniqueConstraint(
            "target_bucket",
            "target_key",
            name=op.f("uq_asset_migration_entries_target_bucket"),
        ),
    )
    op.create_index(
        "ix_asset_migration_entries_manifest_status",
        "asset_migration_entries",
        ["manifest_id", "status"],
        unique=False,
    )

    op.create_table(
        "podcast_episodes",
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("cover_asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("published_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
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
            "(status = 'draft' AND published_at IS NULL AND published_by_user_id IS NULL) OR "
            "(status = 'published' AND published_at IS NOT NULL "
            "AND published_by_user_id IS NOT NULL)",
            name=op.f("ck_podcast_episodes_publication_fields_consistent"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published')",
            name=op.f("ck_podcast_episodes_status_valid"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_podcast_episodes_version_positive")),
        sa.ForeignKeyConstraint(
            ["cover_asset_id"],
            ["assets.id"],
            name=op.f("fk_podcast_episodes_cover_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_podcast_episodes_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["published_by_user_id"],
            ["users.id"],
            name=op.f("fk_podcast_episodes_published_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_podcast_episodes")),
        sa.UniqueConstraint(
            "trading_date",
            name=op.f("uq_podcast_episodes_trading_date"),
        ),
    )
    op.create_index(
        "ix_podcast_episodes_status_date",
        "podcast_episodes",
        ["status", "trading_date"],
        unique=False,
    )

    op.create_table(
        "podcast_episode_translations",
        sa.Column("episode_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
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
            "locale IN ('zh-TW', 'zh-CN', 'en')",
            name=op.f("ck_podcast_episode_translations_locale_supported"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(summary)) > 0",
            name=op.f("ck_podcast_episode_translations_summary_nonempty"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(title)) > 0",
            name=op.f("ck_podcast_episode_translations_title_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["podcast_episodes.id"],
            name=op.f("fk_podcast_episode_translations_episode_id_podcast_episodes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "episode_id",
            "locale",
            name=op.f("pk_podcast_episode_translations"),
        ),
    )

    op.create_table(
        "podcast_episode_audio_variants",
        sa.Column("episode_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("activated_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=True),
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
            "locale IN ('zh-TW', 'zh-CN', 'en')",
            name=op.f("ck_podcast_episode_audio_variants_locale_supported"),
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_podcast_episode_audio_variants_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["activated_by_user_id"],
            ["users.id"],
            name=op.f("fk_podcast_episode_audio_variants_activated_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.id"],
            name=op.f("fk_podcast_episode_audio_variants_asset_id_assets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["podcast_episodes.id"],
            name=op.f("fk_podcast_episode_audio_variants_episode_id_podcast_episodes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_podcast_episode_audio_variants"),
        ),
        sa.UniqueConstraint(
            "episode_id",
            "locale",
            "version",
            name="uq_podcast_episode_audio_variant_version",
        ),
    )
    op.create_index(
        "ix_podcast_episode_audio_variants_asset",
        "podcast_episode_audio_variants",
        ["asset_id"],
        unique=False,
    )
    op.create_index(
        "uq_podcast_episode_audio_variant_active",
        "podcast_episode_audio_variants",
        ["episode_id", "locale"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.execute(
        """
        CREATE FUNCTION reject_retained_history_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'retained chat and model history cannot be deleted';
            END IF;
            IF TG_TABLE_NAME IN ('conversations', 'model_configurations') THEN
                RAISE EXCEPTION 'retained chat and model history is immutable';
            END IF;
            IF OLD.status <> 'pending' THEN
                RAISE EXCEPTION 'terminal chat and generation history is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table in ("conversations", "messages", "generation_records", "model_configurations"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_retained_history
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_retained_history_mutation()
            """
        )


def downgrade() -> None:
    for table in ("model_configurations", "generation_records", "messages", "conversations"):
        op.execute(f"DROP TRIGGER {table}_retained_history ON {table}")
    op.execute("DROP FUNCTION reject_retained_history_mutation()")

    op.drop_table("podcast_episode_audio_variants")
    op.drop_table("podcast_episode_translations")
    op.drop_table("podcast_episodes")
    op.drop_table("asset_migration_entries")
    op.drop_table("asset_migration_manifests")
    op.drop_table("active_model_configuration")
    op.add_column(
        "model_configurations",
        sa.Column("is_active", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_index(
        "uq_model_configurations_single_active",
        "model_configurations",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.drop_constraint(
        op.f("fk_generation_records_message_id_messages"),
        "generation_records",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_generation_records_message_id_messages"),
        "generation_records",
        "messages",
        ["message_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        op.f("fk_messages_conversation_id_conversations"),
        "messages",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_messages_conversation_id_conversations"),
        "messages",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint(
        op.f("ck_assets_active_metadata_complete"),
        "assets",
        type_="check",
    )
    _replace_asset_status(
        ("active", "archived", "deleted"),
        replacement="asset_status_phase2b",
    )
