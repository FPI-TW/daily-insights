from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.schema import DefaultClause

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import LOCAL_DATABASE_URL, Settings
from daily_insights_api.core.enums import AssetKind, GenerationStatus, SystemRole
from daily_insights_api.core.models import Base
from daily_insights_api.modules.markets.catalog import MARKETS

EXPECTED_TABLES = {
    "assets",
    "conversations",
    "generation_records",
    "markets",
    "memberships",
    "messages",
    "model_configurations",
    "organization_market_policies",
    "organizations",
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


def test_conversation_references_membership_pair() -> None:
    conversation_table = Base.metadata.tables["conversations"]
    composite_targets = {
        tuple(foreign_key.target_fullname for foreign_key in constraint.elements)
        for constraint in conversation_table.foreign_key_constraints
    }
    assert ("memberships.organization_id", "memberships.user_id") in composite_targets


def test_only_one_active_model_configuration_index_is_partial() -> None:
    table = Base.metadata.tables["model_configurations"]
    index = next(
        index for index in table.indexes if index.name == "uq_model_configurations_single_active"
    )
    assert index.unique
    assert str(index.dialect_options["postgresql"]["where"]) == "is_active"


def test_model_configuration_is_global_only() -> None:
    columns = Base.metadata.tables["model_configurations"].columns
    assert "organization_id" not in columns
    assert {"provider", "requested_model", "prompt_version", "version", "is_active"} <= set(
        columns.keys()
    )


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


def test_development_has_local_only_database_default() -> None:
    settings = Settings(environment="development")
    assert settings.database_url == LOCAL_DATABASE_URL


def test_initial_migration_is_present() -> None:
    migration = (
        Path(__file__).parents[1] / "migrations" / "versions" / "20260724_0001_initial_schema.py"
    )
    assert migration.is_file()
