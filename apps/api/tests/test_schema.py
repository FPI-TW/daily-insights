from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.schema import DefaultClause

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import LOCAL_DATABASE_URL, Settings
from daily_insights_api.core.enums import AssetKind, GenerationStatus, SystemRole
from daily_insights_api.core.models import Base
from daily_insights_api.modules.markets.catalog import MARKETS

EXPECTED_TABLES = {
    "active_model_configuration",
    "analyst_viewpoints",
    "analyst_viewpoint_sync_runs",
    "assets",
    "asset_migration_entries",
    "asset_migration_manifests",
    "audit_events",
    "conversations",
    "data_management_runs",
    "generation_records",
    "index_daily_bars",
    "index_daily_bar_series",
    "institutional_market_flows",
    "institutional_stock_flows",
    "login_throttles",
    "markets",
    "macro_dashboard_snapshots",
    "memberships",
    "messages",
    "model_configurations",
    "news_editions",
    "news_generation_audits",
    "news_items",
    "news_presentations",
    "organization_market_policies",
    "organizations",
    "podcast_episode_audio_variants",
    "podcast_episode_translations",
    "podcast_episodes",
    "publication_source_runs",
    "report_pipeline_runs",
    "report_publications",
    "sessions",
    "source_runs",
    "users",
}


def test_schema_registers_all_foundation_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_fixed_market_catalog_has_eight_unique_codes() -> None:
    codes = [market.code for market in MARKETS]
    assert len(codes) == 8
    assert len(set(codes)) == 8
    assert all(market.name_en and market.name_zh_hant and market.name_zh_hans for market in MARKETS)


def test_global_roles_and_generation_states_are_stable() -> None:
    assert [role.value for role in SystemRole] == ["admin", "asset_manager", "org_member"]
    assert [status.value for status in GenerationStatus] == [
        "pending",
        "complete",
        "partial",
        "error",
    ]
    assert [kind.value for kind in AssetKind] == ["audio", "image", "downloadable", "pdf"]


def test_email_is_canonicalized_by_database_constraint() -> None:
    user_table = Base.metadata.tables["users"]
    constraint_sql = {
        str(constraint.sqltext)
        for constraint in user_table.constraints
        if hasattr(constraint, "sqltext")
    }
    assert "email = lower(btrim(email))" in constraint_sql
    assert user_table.columns["email"].unique


def test_contract_seat_limit_is_required_and_positive() -> None:
    organization_table = Base.metadata.tables["organizations"]
    constraint_sql = {
        str(constraint.sqltext)
        for constraint in organization_table.constraints
        if hasattr(constraint, "sqltext")
    }
    assert not organization_table.columns["seat_limit"].nullable
    assert "seat_limit > 0" in constraint_sql


def test_admin_provisioned_user_requires_initial_password_change() -> None:
    user_table = Base.metadata.tables["users"]
    password_change_default = user_table.columns["must_change_password"].server_default
    status_default = user_table.columns["status"].server_default
    assert not user_table.columns["password_hash"].nullable
    assert not user_table.columns["must_change_password"].nullable
    assert isinstance(password_change_default, DefaultClause)
    assert str(password_change_default.arg) == "true"
    assert isinstance(status_default, DefaultClause)
    assert str(status_default.arg) == "active"


def test_phase1_sessions_store_only_hashed_tokens() -> None:
    session_table = Base.metadata.tables["sessions"]
    assert {"token_hash", "csrf_token_hash", "expires_at", "revoked_at"} <= set(
        session_table.columns.keys()
    )
    assert "token" not in session_table.columns
    assert session_table.columns["token_hash"].unique


def test_membership_removal_preserves_row_and_active_user_is_unique() -> None:
    membership_table = Base.metadata.tables["memberships"]
    assert {"removed_at", "removed_by_user_id"} <= set(membership_table.columns.keys())
    active_user_index = next(
        index for index in membership_table.indexes if index.name == "uq_memberships_active_user"
    )
    assert active_user_index.unique
    assert str(active_user_index.dialect_options["postgresql"]["where"]) == "removed_at IS NULL"


def test_audit_event_has_actor_target_and_before_after_evidence() -> None:
    columns = Base.metadata.tables["audit_events"].columns
    assert {
        "actor_user_id",
        "organization_id",
        "action",
        "target_type",
        "target_id",
        "reason",
        "before",
        "after",
        "request_id",
        "created_at",
    } <= set(columns.keys())
    assert "updated_at" not in columns


def test_conversation_references_membership_pair() -> None:
    conversation_table = Base.metadata.tables["conversations"]
    composite_targets = {
        tuple(foreign_key.target_fullname for foreign_key in constraint.elements)
        for constraint in conversation_table.foreign_key_constraints
    }
    assert ("memberships.organization_id", "memberships.user_id") in composite_targets


def test_only_one_active_model_configuration_uses_singleton_pointer() -> None:
    pointer = Base.metadata.tables["active_model_configuration"]
    assert set(pointer.primary_key.columns.keys()) == {"singleton_id"}
    constraint_sql = {
        str(constraint.sqltext)
        for constraint in pointer.constraints
        if hasattr(constraint, "sqltext")
    }
    assert "singleton_id = 1" in constraint_sql
    target = next(iter(pointer.columns["model_configuration_id"].foreign_keys))
    assert target.target_fullname == "model_configurations.id"
    assert target.ondelete == "RESTRICT"


def test_model_configuration_is_global_only() -> None:
    columns = Base.metadata.tables["model_configurations"].columns
    assert "organization_id" not in columns
    assert "is_active" not in columns
    assert {"provider", "requested_model", "prompt_version", "version"} <= set(columns.keys())


def test_generation_record_snapshots_reproducibility_fields() -> None:
    columns = Base.metadata.tables["generation_records"].columns
    assert {
        "provider",
        "requested_model",
        "resolved_model",
        "model_configuration_version",
        "prompt_version",
        "context_version",
        "report_version",
        "parameters",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "status",
    } <= set(columns.keys())


def test_generation_model_id_and_version_reference_same_configuration() -> None:
    generation_table = Base.metadata.tables["generation_records"]
    composite_targets = {
        tuple(foreign_key.target_fullname for foreign_key in constraint.elements)
        for constraint in generation_table.foreign_key_constraints
    }
    assert (
        "model_configurations.id",
        "model_configurations.version",
    ) in composite_targets


def test_message_can_exist_without_generation_record() -> None:
    message_id = Base.metadata.tables["generation_records"].columns["message_id"]
    assert not message_id.nullable
    assert message_id.unique
    assert not any(
        foreign_key.column.table.name == "generation_records"
        for foreign_key in Base.metadata.tables["messages"].foreign_keys
    )


def test_absent_market_policy_can_represent_default_visible() -> None:
    policy_table = Base.metadata.tables["organization_market_policies"]
    default = policy_table.columns["is_visible"].server_default
    assert isinstance(default, DefaultClause)
    assert str(default.arg) == "true"
    assert {
        "changed_by_user_id",
        "changed_at",
        "contract_reference",
        "reason",
    } <= set(policy_table.columns.keys())


def test_production_requires_database_url() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production")


@pytest.mark.parametrize(
    ("session_secret", "password_pepper"),
    [
        ("short", "another-short"),
        ("a" * 32, "a" * 32),
        ("CHANGE_ME_" + "a" * 32, "b" * 32),
    ],
)
def test_production_rejects_weak_or_reused_secrets(
    session_secret: str,
    password_pepper: str,
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            database_url="postgresql+psycopg://example.invalid/database",
            session_secret=SecretStr(session_secret),
            password_pepper=SecretStr(password_pepper),
        )


def test_development_has_local_only_database_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAILY_INSIGHTS_DATABASE_URL", raising=False)
    settings = Settings(environment="development", _env_file=None)
    assert settings.database_url == LOCAL_DATABASE_URL


def test_initial_migration_is_present() -> None:
    migration_directory = Path(__file__).parents[1] / "migrations" / "versions"
    assert (migration_directory / "20260724_0001_initial_schema.py").is_file()
    assert (migration_directory / "20260724_0002_phase1_identity.py").is_file()
