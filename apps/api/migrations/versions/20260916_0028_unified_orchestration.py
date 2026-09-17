"""Add unified provider/function/job/routine orchestration.

Revision ID: 20260916_0028
Revises: 20260915_0027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260916_0028"
down_revision: str | None = "20260915_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID = sa.UUID()


def _timestamps() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
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
    )


def upgrade() -> None:
    op.rename_table("data_management_runs", "legacy_data_management_runs")
    op.create_table(
        "routine_runs",
        sa.Column("routine_key", sa.String(100), nullable=False),
        sa.Column("registry_version", sa.String(100), nullable=False),
        sa.Column("registry_digest", sa.String(64), nullable=False),
        sa.Column("registry_snapshot", JSONB, nullable=False),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("result", JSONB),
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','partial','failed','cancelled')",
            name="status_valid",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routine_runs")),
        sa.UniqueConstraint(
            "routine_key",
            "edition_date",
            name="uq_routine_run_edition",
        ),
    )
    op.create_index("ix_routine_runs_created_at", "routine_runs", ["created_at"])

    op.create_table(
        "job_runs",
        sa.Column("routine_run_id", UUID),
        sa.Column("job_key", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("automatic_key", sa.String(100)),
        sa.Column("registry_version", sa.String(100), nullable=False),
        sa.Column("registry_snapshot", JSONB, nullable=False),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("requested_by_user_id", UUID),
        sa.Column("payload", JSONB),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_token", UUID),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("result", JSONB),
        sa.Column("error", sa.String(500)),
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.CheckConstraint("trigger IN ('automatic','manual')", name="trigger_valid"),
        sa.CheckConstraint("kind IN ('function','projection')", name="kind_valid"),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','partial','failed','cancelled')",
            name="status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["routine_run_id"],
            ["routine_runs.id"],
            ondelete="RESTRICT",
            name=op.f("fk_job_runs_routine_run_id_routine_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name=op.f("fk_job_runs_requested_by_user_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_runs")),
    )
    op.create_index(
        "uq_job_runs_automatic_key_edition",
        "job_runs",
        ["automatic_key", "edition_date"],
        unique=True,
        postgresql_where=sa.text("trigger = 'automatic'"),
    )
    op.create_index("ix_job_runs_claim", "job_runs", ["status", "next_attempt_at", "created_at"])
    op.create_index("ix_job_runs_routine", "job_runs", ["routine_run_id", "created_at"])

    op.create_table(
        "job_dependencies",
        sa.Column("upstream_job_run_id", UUID, nullable=False),
        sa.Column("downstream_job_run_id", UUID, nullable=False),
        sa.Column("policy", sa.String(20), nullable=False),
        sa.CheckConstraint("policy IN ('success','terminal')", name="policy_valid"),
        sa.ForeignKeyConstraint(
            ["upstream_job_run_id"],
            ["job_runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_job_dependencies_upstream_job_run_id_job_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["downstream_job_run_id"],
            ["job_runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_job_dependencies_downstream_job_run_id_job_runs"),
        ),
        sa.PrimaryKeyConstraint(
            "upstream_job_run_id", "downstream_job_run_id", name=op.f("pk_job_dependencies")
        ),
    )

    op.create_table(
        "function_runs",
        sa.Column("job_run_id", UUID, nullable=False),
        sa.Column("function_key", sa.String(100), nullable=False),
        sa.Column("provider_key", sa.String(100), nullable=False),
        sa.Column("scope", JSONB, nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("missing_scopes", JSONB),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_token", UUID),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("result", JSONB),
        sa.Column("error", sa.String(500)),
        sa.Column("id", UUID, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending','running','retry_wait','succeeded','no_change','partial',"
            "'unavailable','failed','cancelled')",
            name="status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["job_run_id"],
            ["job_runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_function_runs_job_run_id_job_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_function_runs")),
        sa.UniqueConstraint("job_run_id", "function_key", name="uq_function_run_job_function"),
    )
    op.create_index(
        "ix_function_runs_ready",
        "function_runs",
        ["status", "next_attempt_at", "provider_key"],
    )

    op.create_table(
        "function_dependencies",
        sa.Column("upstream_function_run_id", UUID, nullable=False),
        sa.Column("downstream_function_run_id", UUID, nullable=False),
        sa.Column("policy", sa.String(20), nullable=False),
        sa.CheckConstraint("policy IN ('success','terminal')", name="policy_valid"),
        sa.ForeignKeyConstraint(
            ["upstream_function_run_id"],
            ["function_runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_function_dependencies_upstream_function_run_id_function_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["downstream_function_run_id"],
            ["function_runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_function_dependencies_downstream_function_run_id_function_runs"),
        ),
        sa.PrimaryKeyConstraint(
            "upstream_function_run_id",
            "downstream_function_run_id",
            name=op.f("pk_function_dependencies"),
        ),
    )

    op.create_table(
        "function_attempts",
        sa.Column("function_run_id", UUID, nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider_key", sa.String(100), nullable=False),
        sa.Column("function_key", sa.String(100), nullable=False),
        sa.Column("scope", JSONB, nullable=False),
        sa.Column("fence_token", UUID, nullable=False),
        sa.Column("status", sa.String(20), server_default="running", nullable=False),
        sa.Column("request_metadata", JSONB, nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("source_as_of", sa.Date()),
        sa.Column("fetched_at", sa.DateTime(timezone=True)),
        sa.Column("record_count", sa.Integer()),
        sa.Column("payload_digest", sa.String(64)),
        sa.Column("result", JSONB),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_detail", sa.String(500)),
        sa.Column("id", UUID, nullable=False),
        sa.CheckConstraint("attempt_number > 0", name="attempt_number_positive"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','no_change','partial','unavailable',"
            "'failed','cancelled')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "record_count IS NULL OR record_count >= 0", name="record_count_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["function_run_id"],
            ["function_runs.id"],
            ondelete="CASCADE",
            name=op.f("fk_function_attempts_function_run_id_function_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_function_attempts")),
        sa.UniqueConstraint("function_run_id", "attempt_number", name="uq_function_attempt_number"),
    )
    op.create_index(
        "ix_function_attempts_provider_started",
        "function_attempts",
        ["provider_key", "started_at"],
    )

    _create_typed_storage()
    _create_projection_storage()
    _expand_report_publications()
    _expand_macro_dashboard_snapshot()
    _expand_news()
    _expand_analyst_viewpoints()
    _create_immutability_guards()


def _create_typed_storage() -> None:
    for (
        table,
        observation_table,
        series_unique,
        observation_unique,
        observation_index,
        value_columns,
    ) in (
        (
            "market_daily_series",
            "market_daily_observations",
            "uq_market_daily_series",
            "uq_market_observation_version",
            "ix_market_observation_latest",
            (
                sa.Column("open", sa.Numeric(24, 10)),
                sa.Column("high", sa.Numeric(24, 10)),
                sa.Column("low", sa.Numeric(24, 10)),
                sa.Column("close", sa.Numeric(24, 10), nullable=False),
                sa.Column("volume", sa.BigInteger()),
            ),
        ),
        (
            "interest_rate_series",
            "interest_rate_observations",
            "uq_interest_rate_series",
            "uq_interest_rate_observation_version",
            "ix_interest_rate_observation_latest",
            (sa.Column("value", sa.Numeric(18, 8), nullable=False),),
        ),
    ):
        op.create_table(
            table,
            sa.Column("provider_key", sa.String(100), nullable=False),
            sa.Column("dataset_key", sa.String(100), nullable=False),
            sa.Column("symbol", sa.String(100), nullable=False),
            sa.Column("market", sa.String(50), nullable=False),
            sa.Column("unit", sa.String(50), nullable=False),
            sa.Column("contract_version", sa.String(100), nullable=False),
            sa.Column("id", UUID, nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
            sa.UniqueConstraint("provider_key", "dataset_key", "symbol", name=series_unique),
        )
        op.create_table(
            observation_table,
            sa.Column("series_id", UUID, nullable=False),
            sa.Column("function_attempt_id", UUID, nullable=False),
            sa.Column("observation_date", sa.Date(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            *value_columns,
            sa.Column("source_timestamp", sa.DateTime(timezone=True)),
            sa.Column("value_digest", sa.String(64), nullable=False),
            sa.Column("id", UUID, nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint("version > 0", name="version_positive"),
            sa.CheckConstraint("char_length(value_digest) = 64", name="value_digest_sha256"),
            sa.ForeignKeyConstraint(
                ["series_id"],
                [f"{table}.id"],
                ondelete="RESTRICT",
                name=op.f(f"fk_{observation_table}_series_id_{table}"),
            ),
            sa.ForeignKeyConstraint(
                ["function_attempt_id"],
                ["function_attempts.id"],
                ondelete="RESTRICT",
                name=op.f(f"fk_{observation_table}_function_attempt_id_function_attempts"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{observation_table}")),
            sa.UniqueConstraint(
                "series_id",
                "observation_date",
                "version",
                name=observation_unique,
            ),
        )
        op.create_index(
            observation_index,
            observation_table,
            ["series_id", "observation_date", "version"],
        )


def _create_projection_storage() -> None:
    op.create_table(
        "projection_input_freezes",
        sa.Column("projection_job_run_id", UUID, nullable=False),
        sa.Column("registry_version", sa.String(100), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("inputs", JSONB, nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("char_length(input_digest) = 64", name="input_digest_sha256"),
        sa.ForeignKeyConstraint(
            ["projection_job_run_id"],
            ["job_runs.id"],
            ondelete="RESTRICT",
            name=op.f("fk_projection_input_freezes_projection_job_run_id_job_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projection_input_freezes")),
        sa.UniqueConstraint("projection_job_run_id", name="uq_projection_input_freeze_job"),
    )
    op.create_table(
        "projection_input_observations",
        sa.Column("freeze_id", UUID, nullable=False),
        sa.Column("observation_kind", sa.String(20), nullable=False),
        sa.Column("observation_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["freeze_id"],
            ["projection_input_freezes.id"],
            ondelete="CASCADE",
            name=op.f("fk_projection_input_observations_freeze_id_projection_input_freezes"),
        ),
        sa.PrimaryKeyConstraint(
            "freeze_id",
            "observation_kind",
            "observation_id",
            name=op.f("pk_projection_input_observations"),
        ),
    )
    op.create_table(
        "publication_function_attempts",
        sa.Column("publication_id", UUID, nullable=False),
        sa.Column("function_attempt_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["report_publications.id"],
            ondelete="RESTRICT",
            name=op.f("fk_publication_function_attempts_publication_id_report_publications"),
        ),
        sa.ForeignKeyConstraint(
            ["function_attempt_id"],
            ["function_attempts.id"],
            ondelete="RESTRICT",
            name=op.f("fk_publication_function_attempts_function_attempt_id_function_attempts"),
        ),
        sa.PrimaryKeyConstraint(
            "publication_id", "function_attempt_id", name=op.f("pk_publication_function_attempts")
        ),
    )


def _expand_report_publications() -> None:
    op.alter_column("report_publications", "pipeline_run_id", nullable=True)
    op.add_column("report_publications", sa.Column("projection_job_run_id", UUID))
    op.create_foreign_key(
        op.f("fk_report_publications_projection_job_run_id_job_runs"),
        "report_publications",
        "job_runs",
        ["projection_job_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "exactly_one_owner",
        "report_publications",
        "(pipeline_run_id IS NULL) <> (projection_job_run_id IS NULL)",
    )


def _expand_macro_dashboard_snapshot() -> None:
    op.add_column("macro_dashboard_snapshots", sa.Column("input_digest", sa.String(64)))
    op.add_column(
        "macro_dashboard_snapshots",
        sa.Column(
            "source_references",
            JSONB,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("macro_dashboard_snapshots", sa.Column("projection_job_run_id", UUID))
    op.create_foreign_key(
        op.f("fk_macro_dashboard_snapshots_projection_job_run_id_job_runs"),
        "macro_dashboard_snapshots",
        "job_runs",
        ["projection_job_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def _expand_news() -> None:
    op.create_table(
        "news_candidate_batches",
        sa.Column("function_attempt_id", UUID, nullable=False),
        sa.Column("edition_date", sa.Date(), nullable=False),
        sa.Column("market_code", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), server_default="collecting", nullable=False),
        sa.Column("input_digest", sa.String(64)),
        sa.Column("source_as_of", sa.DateTime(timezone=True)),
        sa.Column("result", JSONB),
        sa.Column("id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "market_code IN ('global','tw_equity','us_equity')", name="market_code_valid"
        ),
        sa.CheckConstraint(
            "status IN ('collecting','ready','partial','unavailable','failed','cancelled')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "input_digest IS NULL OR char_length(input_digest) = 64", name="input_digest_sha256"
        ),
        sa.ForeignKeyConstraint(
            ["function_attempt_id"],
            ["function_attempts.id"],
            ondelete="RESTRICT",
            name=op.f("fk_news_candidate_batches_function_attempt_id_function_attempts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_candidate_batches")),
        sa.UniqueConstraint("function_attempt_id", name="uq_news_candidate_batch_attempt"),
    )
    op.create_index(
        "ix_news_candidate_batches_market_date",
        "news_candidate_batches",
        ["market_code", "edition_date", "created_at"],
    )
    op.add_column("news_editions", sa.Column("candidate_batch_id", UUID))
    op.create_foreign_key(
        op.f("fk_news_editions_candidate_batch_id_news_candidate_batches"),
        "news_editions",
        "news_candidate_batches",
        ["candidate_batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("news_editions", sa.Column("publication_job_run_id", UUID))
    op.create_foreign_key(
        op.f("fk_news_editions_publication_job_run_id_job_runs"),
        "news_editions",
        "job_runs",
        ["publication_job_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("news_candidates", sa.Column("batch_id", UUID))
    op.create_foreign_key(
        op.f("fk_news_candidates_batch_id_news_candidate_batches"),
        "news_candidates",
        "news_candidate_batches",
        ["batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_news_candidates_batch_id", "news_candidates", ["batch_id"])
    op.alter_column("news_candidates", "edition_id", nullable=True)
    op.create_check_constraint(
        "exactly_one_owner",
        "news_candidates",
        "(edition_id IS NULL) <> (batch_id IS NULL)",
    )
    op.create_index(
        "uq_news_candidate_batch",
        "news_candidates",
        ["batch_id", "candidate_id"],
        unique=True,
        postgresql_where=sa.text("batch_id IS NOT NULL"),
    )
    op.add_column("news_generation_audits", sa.Column("candidate_batch_id", UUID))
    op.create_foreign_key(
        op.f("fk_news_generation_audits_candidate_batch_id_news_candidate_batches"),
        "news_generation_audits",
        "news_candidate_batches",
        ["candidate_batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.alter_column("news_generation_audits", "edition_id", nullable=True)
    op.create_check_constraint(
        "exactly_one_owner",
        "news_generation_audits",
        "(edition_id IS NULL) <> (candidate_batch_id IS NULL)",
    )
    op.create_table(
        "prepared_news_items",
        sa.Column("batch_id", UUID, nullable=False),
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("topic", sa.String(50), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("market", sa.String(20)),
        sa.Column("event_key", sa.String(80)),
        sa.Column("numeric_facts", JSONB, nullable=False),
        sa.Column("presentations", JSONB, nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("rank > 0", name="rank_positive"),
        sa.CheckConstraint("char_length(content_digest) = 64", name="content_digest_sha256"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["news_candidate_batches.id"],
            ondelete="RESTRICT",
            name=op.f("fk_prepared_news_items_batch_id_news_candidate_batches"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["news_candidates.id"],
            ondelete="RESTRICT",
            name=op.f("fk_prepared_news_items_candidate_id_news_candidates"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prepared_news_items")),
        sa.UniqueConstraint("batch_id", "rank", name="uq_prepared_news_item_rank"),
        sa.UniqueConstraint("batch_id", "candidate_id", name="uq_prepared_news_item_candidate"),
    )
    op.create_table(
        "news_candidate_publications",
        sa.Column("publish_job_run_id", UUID, nullable=False),
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("item_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["publish_job_run_id"],
            ["job_runs.id"],
            ondelete="RESTRICT",
            name=op.f("fk_news_candidate_publications_publish_job_run_id_job_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["news_candidates.id"],
            ondelete="RESTRICT",
            name=op.f("fk_news_candidate_publications_candidate_id_news_candidates"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["news_items.id"],
            ondelete="RESTRICT",
            name=op.f("fk_news_candidate_publications_item_id_news_items"),
        ),
        sa.PrimaryKeyConstraint(
            "publish_job_run_id", "candidate_id", name=op.f("pk_news_candidate_publications")
        ),
        sa.UniqueConstraint(
            "publish_job_run_id", "candidate_id", name="uq_news_candidate_publish_job"
        ),
    )


def _expand_analyst_viewpoints() -> None:
    op.create_table(
        "analyst_viewpoint_versions",
        sa.Column("viewpoint_date", sa.Date(), nullable=False),
        sa.Column("market_code", sa.String(50), nullable=False),
        sa.Column("source_market_code", sa.String(50), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("points", JSONB, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("function_attempt_id", UUID, nullable=False),
        sa.Column("id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint("jsonb_typeof(points) = 'array'", name="points_are_array"),
        sa.CheckConstraint("char_length(content_digest) = 64", name="content_digest_sha256"),
        sa.ForeignKeyConstraint(
            ["market_code"],
            ["markets.code"],
            ondelete="RESTRICT",
            name=op.f("fk_analyst_viewpoint_versions_market_code_markets"),
        ),
        sa.ForeignKeyConstraint(
            ["function_attempt_id"],
            ["function_attempts.id"],
            ondelete="RESTRICT",
            name=op.f("fk_analyst_viewpoint_versions_function_attempt_id_function_attempts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analyst_viewpoint_versions")),
        sa.UniqueConstraint(
            "viewpoint_date", "market_code", "version", name="uq_analyst_viewpoint_version"
        ),
    )
    op.create_index(
        "ix_analyst_viewpoint_versions_latest",
        "analyst_viewpoint_versions",
        ["viewpoint_date", "market_code", "version"],
    )
    op.add_column("analyst_viewpoints", sa.Column("current_version_id", UUID))
    op.create_foreign_key(
        op.f("fk_analyst_viewpoints_current_version_id_analyst_viewpoint_versions"),
        "analyst_viewpoints",
        "analyst_viewpoint_versions",
        ["current_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("analyst_viewpoint_sync_runs", sa.Column("function_attempt_id", UUID))
    op.create_foreign_key(
        op.f("fk_analyst_viewpoint_sync_runs_function_attempt_id_function_attempts"),
        "analyst_viewpoint_sync_runs",
        "function_attempts",
        ["function_attempt_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def _create_immutability_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_orchestration_fact_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'orchestration facts and provenance are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "market_daily_observations",
        "interest_rate_observations",
        "projection_input_freezes",
        "projection_input_observations",
        "publication_function_attempts",
        "prepared_news_items",
        "news_candidate_publications",
        "analyst_viewpoint_versions",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_are_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_orchestration_fact_mutation()"
        )
    op.execute(
        "CREATE TRIGGER legacy_data_management_runs_are_read_only "
        "BEFORE INSERT OR UPDATE OR DELETE ON legacy_data_management_runs "
        "FOR EACH ROW EXECUTE FUNCTION reject_orchestration_fact_mutation()"
    )


def downgrade() -> None:
    guarded_tables = (
        "routine_runs",
        "job_runs",
        "function_runs",
        "function_attempts",
        "market_daily_observations",
        "interest_rate_observations",
        "news_candidate_batches",
        "analyst_viewpoint_versions",
    )
    checks = " OR ".join(f"EXISTS (SELECT 1 FROM {table})" for table in guarded_tables)
    op.execute(
        sa.text(
            "DO $$ BEGIN IF " + checks + " THEN RAISE EXCEPTION "
            "'cannot downgrade while unified orchestration data exists'; END IF; END $$"
        )
    )
    op.execute(
        "DROP TRIGGER legacy_data_management_runs_are_read_only ON legacy_data_management_runs"
    )
    for table in (
        "analyst_viewpoint_versions",
        "news_candidate_publications",
        "prepared_news_items",
        "publication_function_attempts",
        "projection_input_observations",
        "projection_input_freezes",
        "interest_rate_observations",
        "market_daily_observations",
    ):
        op.execute(f"DROP TRIGGER {table}_are_immutable ON {table}")
    op.execute("DROP FUNCTION reject_orchestration_fact_mutation()")

    op.drop_constraint(
        op.f("fk_analyst_viewpoint_sync_runs_function_attempt_id_function_attempts"),
        "analyst_viewpoint_sync_runs",
        type_="foreignkey",
    )
    op.drop_column("analyst_viewpoint_sync_runs", "function_attempt_id")
    op.drop_constraint(
        op.f("fk_analyst_viewpoints_current_version_id_analyst_viewpoint_versions"),
        "analyst_viewpoints",
        type_="foreignkey",
    )
    op.drop_column("analyst_viewpoints", "current_version_id")
    op.drop_index("ix_analyst_viewpoint_versions_latest", table_name="analyst_viewpoint_versions")
    op.drop_table("analyst_viewpoint_versions")

    op.drop_table("news_candidate_publications")
    op.drop_table("prepared_news_items")
    op.drop_constraint(
        op.f("fk_news_editions_publication_job_run_id_job_runs"),
        "news_editions",
        type_="foreignkey",
    )
    op.drop_column("news_editions", "publication_job_run_id")
    op.drop_constraint(
        op.f("fk_news_editions_candidate_batch_id_news_candidate_batches"),
        "news_editions",
        type_="foreignkey",
    )
    op.drop_column("news_editions", "candidate_batch_id")
    op.drop_constraint(
        op.f("ck_news_generation_audits_exactly_one_owner"),
        "news_generation_audits",
        type_="check",
    )
    op.alter_column("news_generation_audits", "edition_id", nullable=False)
    op.drop_constraint(
        op.f("fk_news_generation_audits_candidate_batch_id_news_candidate_batches"),
        "news_generation_audits",
        type_="foreignkey",
    )
    op.drop_column("news_generation_audits", "candidate_batch_id")
    op.drop_index("uq_news_candidate_batch", table_name="news_candidates")
    op.drop_constraint(
        op.f("ck_news_candidates_exactly_one_owner"), "news_candidates", type_="check"
    )
    op.alter_column("news_candidates", "edition_id", nullable=False)
    op.drop_index("ix_news_candidates_batch_id", table_name="news_candidates")
    op.drop_constraint(
        op.f("fk_news_candidates_batch_id_news_candidate_batches"),
        "news_candidates",
        type_="foreignkey",
    )
    op.drop_column("news_candidates", "batch_id")
    op.drop_index("ix_news_candidate_batches_market_date", table_name="news_candidate_batches")
    op.drop_table("news_candidate_batches")

    op.drop_constraint(
        op.f("fk_macro_dashboard_snapshots_projection_job_run_id_job_runs"),
        "macro_dashboard_snapshots",
        type_="foreignkey",
    )
    op.drop_column("macro_dashboard_snapshots", "projection_job_run_id")
    op.drop_column("macro_dashboard_snapshots", "source_references")
    op.drop_column("macro_dashboard_snapshots", "input_digest")

    op.drop_constraint(
        op.f("ck_report_publications_exactly_one_owner"),
        "report_publications",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_report_publications_projection_job_run_id_job_runs"),
        "report_publications",
        type_="foreignkey",
    )
    op.drop_column("report_publications", "projection_job_run_id")
    op.alter_column("report_publications", "pipeline_run_id", nullable=False)

    op.drop_table("publication_function_attempts")
    op.drop_table("projection_input_observations")
    op.drop_table("projection_input_freezes")
    for table in (
        "interest_rate_observations",
        "interest_rate_series",
        "market_daily_observations",
        "market_daily_series",
    ):
        op.drop_table(table)
    op.drop_index("ix_function_attempts_provider_started", table_name="function_attempts")
    op.drop_table("function_attempts")
    op.drop_table("function_dependencies")
    op.drop_index("ix_function_runs_ready", table_name="function_runs")
    op.drop_table("function_runs")
    op.drop_table("job_dependencies")
    op.drop_index("ix_job_runs_routine", table_name="job_runs")
    op.drop_index("ix_job_runs_claim", table_name="job_runs")
    op.drop_index("uq_job_runs_automatic_key_edition", table_name="job_runs")
    op.drop_table("job_runs")
    op.drop_index("ix_routine_runs_created_at", table_name="routine_runs")
    op.drop_table("routine_runs")
    op.rename_table("legacy_data_management_runs", "data_management_runs")
