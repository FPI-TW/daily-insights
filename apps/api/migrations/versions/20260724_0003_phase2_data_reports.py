"""Add Phase 2 source provenance and immutable report publications.

Revision ID: 20260724_0003
Revises: 20260724_0002
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260724_0003"
down_revision: str | None = "20260724_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_pipeline_runs",
        sa.Column("report_key", sa.String(length=100), nullable=False),
        sa.Column("market_code", sa.String(length=50), nullable=False),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("derivation_version", sa.String(length=100), nullable=False),
        sa.Column("content_schema_version", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_detail", sa.String(length=1000), nullable=True),
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
            "attempt_count >= 0",
            name=op.f("ck_report_pipeline_runs_attempt_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) = 64",
            name=op.f("ck_report_pipeline_runs_idempotency_key_sha256"),
        ),
        sa.CheckConstraint(
            "revision > 0",
            name=op.f("ck_report_pipeline_runs_revision_positive"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND started_at IS NULL AND finished_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'published' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND error_code IS NULL) OR "
            "(status = 'failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name=op.f("ck_report_pipeline_runs_status_fields_consistent"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'published', 'failed')",
            name=op.f("ck_report_pipeline_runs_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["market_code"],
            ["markets.code"],
            name=op.f("fk_report_pipeline_runs_market_code_markets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_pipeline_runs")),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_report_pipeline_runs_idempotency_key"),
        ),
        sa.UniqueConstraint(
            "report_key",
            "market_code",
            "edition_date",
            "revision",
            name="uq_report_pipeline_run_version",
        ),
    )
    op.create_index(
        "ix_report_pipeline_runs_claim",
        "report_pipeline_runs",
        ["status", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_report_pipeline_runs_market_edition",
        "report_pipeline_runs",
        ["market_code", "edition_date"],
        unique=False,
    )

    op.create_table(
        "source_runs",
        sa.Column("pipeline_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("dataset_key", sa.String(length=100), nullable=False),
        sa.Column("pipeline_attempt", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="running", nullable=False),
        sa.Column("contract_version", sa.String(length=100), nullable=False),
        sa.Column("contract_hash", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=255), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source_as_of", sa.Date(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_detail", sa.String(length=1000), nullable=True),
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
            "attempt > 0",
            name=op.f("ck_source_runs_attempt_positive"),
        ),
        sa.CheckConstraint(
            "char_length(contract_hash) = 64",
            name=op.f("ck_source_runs_contract_hash_sha256"),
        ),
        sa.CheckConstraint(
            "pipeline_attempt > 0",
            name=op.f("ck_source_runs_pipeline_attempt_positive"),
        ),
        sa.CheckConstraint(
            "payload_sha256 IS NULL OR char_length(payload_sha256) = 64",
            name=op.f("ck_source_runs_payload_digest_sha256"),
        ),
        sa.CheckConstraint(
            "record_count IS NULL OR record_count >= 0",
            name=op.f("ck_source_runs_record_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name=op.f("ck_source_runs_request_fingerprint_sha256"),
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND source_as_of IS NULL "
            "AND record_count IS NULL AND payload_sha256 IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND source_as_of IS NOT NULL "
            "AND record_count IS NOT NULL AND payload_sha256 IS NOT NULL "
            "AND error_code IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL "
            "AND source_as_of IS NULL AND record_count IS NULL AND payload_sha256 IS NULL)",
            name=op.f("ck_source_runs_status_fields_consistent"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name=op.f("ck_source_runs_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["report_pipeline_runs.id"],
            name=op.f("fk_source_runs_pipeline_run_id_report_pipeline_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_runs")),
        sa.UniqueConstraint(
            "pipeline_run_id",
            "dataset_key",
            "pipeline_attempt",
            "attempt",
            name="uq_source_run_attempt",
        ),
    )
    op.create_index(
        op.f("ix_source_runs_pipeline_run_id"),
        "source_runs",
        ["pipeline_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_source_runs_provenance",
        "source_runs",
        ["provider", "dataset_key", "source_as_of"],
        unique=False,
        postgresql_where=sa.text("status = 'succeeded'"),
    )
    op.create_index(
        "ix_source_runs_status_started",
        "source_runs",
        ["status", "started_at"],
        unique=False,
    )

    op.create_table(
        "report_publications",
        sa.Column("pipeline_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_key", sa.String(length=100), nullable=False),
        sa.Column("market_code", sa.String(length=50), nullable=False),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("derivation_version", sa.String(length=100), nullable=False),
        sa.Column("content_schema_version", sa.String(length=100), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("source_as_of", sa.Date(), nullable=False),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "presentations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "published_at",
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
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name=op.f("ck_report_publications_content_is_object"),
        ),
        sa.CheckConstraint(
            "char_length(input_digest) = 64",
            name=op.f("ck_report_publications_input_digest_sha256"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(presentations) = 'object' "
            "AND presentations ?& ARRAY['zh-TW', 'zh-CN', 'en'] "
            "AND presentations - ARRAY['zh-TW', 'zh-CN', 'en'] = '{}'::jsonb",
            name=op.f("ck_report_publications_presentations_have_supported_locales"),
        ),
        sa.CheckConstraint(
            "revision > 0",
            name=op.f("ck_report_publications_revision_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["market_code"],
            ["markets.code"],
            name=op.f("fk_report_publications_market_code_markets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["report_pipeline_runs.id"],
            name=op.f("fk_report_publications_pipeline_run_id_report_pipeline_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_publications")),
        sa.UniqueConstraint(
            "pipeline_run_id",
            name=op.f("uq_report_publications_pipeline_run_id"),
        ),
        sa.UniqueConstraint(
            "report_key",
            "market_code",
            "edition_date",
            "revision",
            name="uq_report_publication_version",
        ),
    )
    op.create_index(
        "ix_report_publications_latest",
        "report_publications",
        ["report_key", "market_code", "edition_date", "revision"],
        unique=False,
    )

    op.create_table(
        "publication_source_runs",
        sa.Column("publication_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["report_publications.id"],
            name=op.f("fk_publication_source_runs_publication_id_report_publications"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["source_runs.id"],
            name=op.f("fk_publication_source_runs_source_run_id_source_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "publication_id",
            "source_run_id",
            name=op.f("pk_publication_source_runs"),
        ),
    )
    op.execute(
        """
        CREATE FUNCTION reject_immutable_report_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'published reports and provenance links are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER report_publications_are_immutable
        BEFORE UPDATE OR DELETE ON report_publications
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_report_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER publication_source_runs_are_immutable
        BEFORE UPDATE OR DELETE ON publication_source_runs
        FOR EACH ROW EXECUTE FUNCTION reject_immutable_report_mutation()
        """
    )


def downgrade() -> None:
    op.drop_table("publication_source_runs")
    op.drop_table("report_publications")
    op.drop_table("source_runs")
    op.drop_table("report_pipeline_runs")
    op.execute("DROP FUNCTION reject_immutable_report_mutation()")
