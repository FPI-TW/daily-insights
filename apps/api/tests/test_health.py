from collections.abc import Awaitable, Callable

import pytest
from httpx import ASGITransport, AsyncClient

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
    assert response.json() == {"status": "ok" if ready else "unhealthy"}


async def test_liveness_does_not_require_database() -> None:
    app = create_app(Settings(environment="test"), readiness(False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
        "/api/auth/change-password",
        "/api/auth/logout",
        "/api/admin/organizations",
        "/api/admin/internal-users",
        "/api/markets",
    } <= paths
    assert not any(
        "register" in path or "forgot" in path or "reset-password" in path for path in paths
    )
