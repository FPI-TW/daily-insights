from collections.abc import Awaitable, Callable

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from daily_insights_api.core.config import Settings
from daily_insights_api.web.app import create_app


def readiness(value: bool) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return value

    return check


@pytest.mark.parametrize(("ready", "expected_status"), [(True, 200), (False, 503)])
async def test_readiness_reflects_database_state(ready: bool, expected_status: int) -> None:
    app = create_app(Settings(environment="test"), readiness(ready))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health/ready")

    assert response.status_code == expected_status
    expected = "ok" if ready else "unhealthy"
    assert response.json() == {
        "status": expected,
        "components": {
            "database": {"status": expected},
            "twelve_data_configuration": {"status": "ok"},
            "daily_news_configuration": {"status": "ok"},
            "chat_configuration": {"status": "ok"},
            "r2_runtime": {"status": "ok"},
        },
    }


async def test_liveness_does_not_require_database() -> None:
    app = create_app(Settings(environment="test"), readiness(False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_production_readiness_reports_initialized_external_boundaries() -> None:
    settings = Settings(
        environment="production",
        database_url="postgresql+psycopg://app:secret@example.invalid/app",
        session_secret=SecretStr("s" * 32),
        password_pepper=SecretStr("p" * 32),
        findb_api_key=SecretStr("findb-production-key"),
        morning_reports_enabled=False,
        r2_endpoint_url="https://account.r2.cloudflarestorage.com",
        r2_bucket_name="daily-insights-production",
        r2_access_key_id=SecretStr("r2-access-key"),
        r2_secret_access_key=SecretStr("r2-secret-key"),
    )
    app = create_app(settings, readiness(True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["components"] == {
        "database": {"status": "ok"},
        "twelve_data_configuration": {"status": "ok"},
        "daily_news_configuration": {"status": "ok"},
        "chat_configuration": {"status": "ok"},
        "r2_runtime": {"status": "ok"},
    }


@pytest.mark.parametrize("length", [100, 101])
async def test_request_id_is_bounded_before_reaching_audit_storage(length: int) -> None:
    app = create_app(Settings(environment="test"), readiness(True))
    supplied = "r" * length
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health/live", headers={"X-Request-ID": supplied})

    assert len(response.headers["X-Request-ID"]) <= 100
    assert (response.headers["X-Request-ID"] == supplied) is (length == 100)


async def test_phase1_openapi_exposes_only_supported_identity_flows() -> None:
    app = create_app(Settings(environment="test"), readiness(True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        document = (await client.get("/openapi.json")).json()

    paths = set(document["paths"])
    assert {
        "/api/auth/login",
        "/api/auth/me",
        "/api/auth/csrf",
        "/api/auth/change-password",
        "/api/auth/logout",
        "/api/admin/organizations",
        "/api/admin/internal-users",
        "/api/markets",
        "/api/reports",
        "/api/reports/{market_code}/latest",
        "/api/news/latest",
    } <= paths
    assert not any(
        "register" in path or "forgot" in path or "reset-password" in path for path in paths
    )
    assert document["paths"]["/api/auth/login"]["post"]["operationId"] == "auth_login"
    assert document["paths"]["/api/auth/csrf"]["post"]["operationId"] == ("auth_rotate_csrf_token")
