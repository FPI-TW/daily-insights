from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

import daily_insights_api.models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings
from daily_insights_api.core.models import Base
from daily_insights_api.modules.data_management.schemas import (
    DataManagementRunCreate,
    DataManagementRunResponse,
)
from daily_insights_api.web.app import create_app

adapter: TypeAdapter[DataManagementRunCreate] = TypeAdapter(DataManagementRunCreate)
response_adapter: TypeAdapter[DataManagementRunResponse] = TypeAdapter(DataManagementRunResponse)


@pytest.mark.parametrize(
    ("payload", "operation"),
    [
        ({"operation": "morning_all"}, "morning_all"),
        ({"operation": "morning_market", "market_code": "crypto"}, "morning_market"),
        ({"operation": "index_yahoo"}, "index_yahoo"),
        ({"operation": "institutional_twse"}, "institutional_twse"),
        ({"operation": "news_all"}, "news_all"),
        ({"operation": "news_market", "market_code": "global"}, "news_market"),
    ],
)
def test_run_create_discriminator_accepts_only_valid_scope(
    payload: dict[str, str], operation: str
) -> None:
    assert adapter.validate_python(payload).operation == operation


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "morning_all", "market_code": "crypto"},
        {"operation": "morning_market"},
        {"operation": "morning_market", "market_code": "tw_equity"},
        {"operation": "index_yahoo", "market_code": "crypto"},
        {"operation": "news_all", "market_code": "global"},
        {"operation": "news_market"},
    ],
)
def test_run_create_discriminator_rejects_invalid_scope(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        adapter.validate_python(payload)


def test_run_response_discriminator_preserves_market_scope() -> None:
    base = {
        "id": "08a70c25-7e41-4dce-a576-fc56589e1db3",
        "edition_date": "2026-09-07",
        "status": "succeeded",
        "requested_by_user_id": "ee77eab0-3910-4706-803c-ffaf979f1ff7",
        "created_at": "2026-09-07T00:00:00Z",
        "started_at": None,
        "completed_at": None,
        "result": None,
        "error": None,
    }
    response = response_adapter.validate_python(
        {**base, "operation": "morning_market", "market_code": "crypto"}
    )
    assert response.operation == "morning_market" and response.market_code == "crypto"
    with pytest.raises(ValidationError):
        response_adapter.validate_python(
            {**base, "operation": "index_yahoo", "market_code": "crypto"}
        )
    news_response = response_adapter.validate_python(
        {**base, "operation": "news_market", "market_code": "global"}
    )
    assert news_response.operation == "news_market" and news_response.market_code == "global"


def test_openapi_declares_discriminated_responses_conflicts_and_legacy_deprecation() -> None:
    schema = create_app(Settings(environment="test")).openapi()
    run_post = schema["paths"]["/api/admin/data-management/runs"]["post"]
    run_get = schema["paths"]["/api/admin/data-management/runs"]["get"]
    assert {"202", "409", "503"} <= set(run_post["responses"])
    response_schema = run_post["responses"]["202"]["content"]["application/json"]["schema"]
    assert response_schema["discriminator"]["propertyName"] == "operation"
    operation_group = next(
        parameter for parameter in run_get["parameters"] if parameter["name"] == "operation_group"
    )
    assert operation_group["schema"]["anyOf"][0]["const"] == "news"
    assert (
        schema["paths"]["/api/admin/data-sources/yfinance/daily-bars"]["post"]["deprecated"] is True
    )


def test_run_market_scope_allows_global_news_without_a_market_catalog_foreign_key() -> None:
    table = Base.metadata.tables["data_management_runs"]
    checks = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if hasattr(constraint, "sqltext")
    }
    assert all(foreign_key.column.table.name != "markets" for foreign_key in table.foreign_keys)
    assert any("'global', 'tw_equity', 'us_equity'" in check for check in checks)
    assert any("'global_macro_bonds', 'crypto', 'us_equity'" in check for check in checks)

    migration = (
        Path(__file__).parents[1] / "migrations/versions/20260907_0020_data_management_news_runs.py"
    ).read_text()
    assert "drop_constraint" in migration
    assert "fk_data_management_runs_market_code_markets" in migration
    assert "market_code_valid_for_operation" in migration


def test_news_migration_downgrade_preflights_without_deleting_runs() -> None:
    migration = (
        Path(__file__).parents[1] / "migrations/versions/20260907_0020_data_management_news_runs.py"
    ).read_text()
    downgrade = migration[migration.index("def downgrade()") :]

    assert "DELETE FROM" not in downgrade.upper()
    preflight = downgrade.index("cannot downgrade data-management schema")
    first_schema_mutation = downgrade.index("op.drop_index")
    assert preflight < first_schema_mutation
    assert "SELECT 1" in downgrade
    assert "operation IN ('news_all', 'news_market')" in downgrade
    assert "RAISE EXCEPTION" in downgrade
    assert "_drop_market_scope_check()" in downgrade
    assert "market_scope_matches_operation" in downgrade
    assert "fk_data_management_runs_market_code_markets" in downgrade
