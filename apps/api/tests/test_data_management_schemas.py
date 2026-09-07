import pytest
from pydantic import TypeAdapter, ValidationError

from daily_insights_api.core.config import Settings
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


def test_openapi_declares_discriminated_responses_conflicts_and_legacy_deprecation() -> None:
    schema = create_app(Settings(environment="test")).openapi()
    run_post = schema["paths"]["/api/admin/data-management/runs"]["post"]
    assert {"202", "409", "503"} <= set(run_post["responses"])
    response_schema = run_post["responses"]["202"]["content"]["application/json"]["schema"]
    assert response_schema["discriminator"]["propertyName"] == "operation"
    assert (
        schema["paths"]["/api/admin/data-sources/yfinance/daily-bars"]["post"]["deprecated"] is True
    )
