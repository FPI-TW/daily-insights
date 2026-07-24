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
