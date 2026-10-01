"""Permission and request-shape checks for /api/admin/newsroom that need no database."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_health import readiness

from daily_insights_api.core.config import LOCAL_SESSION_SECRET, Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.security import hash_token
from daily_insights_api.modules.identity.api import AuthContext
from daily_insights_api.modules.identity.auth import get_auth_context
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.web.app import create_app
from daily_insights_api.web.dependencies import get_database_session

CSRF_TOKEN = "csrf-token"
SOME_ID = "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e11"

READS = [
    "/api/admin/newsroom/sources",
    "/api/admin/newsroom/editions",
    "/api/admin/newsroom/editions/2026-10-01/global",
    f"/api/admin/newsroom/events/{SOME_ID}",
]
WRITES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", "/api/admin/newsroom/sources", {}),
    ("PATCH", f"/api/admin/newsroom/sources/{SOME_ID}", {}),
    ("POST", f"/api/admin/newsroom/editions/{SOME_ID}/publish", None),
    ("POST", "/api/admin/newsroom/editions/publish", {"edition_date": "2026-10-01"}),
    ("POST", f"/api/admin/newsroom/editions/{SOME_ID}/items", {"event_id": SOME_ID}),
    ("PUT", f"/api/admin/newsroom/editions/{SOME_ID}/order", {"item_ids": [SOME_ID]}),
    ("POST", f"/api/admin/newsroom/items/{SOME_ID}/remove", None),
    ("POST", f"/api/admin/newsroom/items/{SOME_ID}/restore", None),
    ("POST", f"/api/admin/newsroom/items/{SOME_ID}/hide", None),
    ("POST", f"/api/admin/newsroom/items/{SOME_ID}/unhide", None),
    ("PUT", f"/api/admin/newsroom/items/{SOME_ID}/why", {"why": "x"}),
    ("PATCH", f"/api/admin/newsroom/events/{SOME_ID}", {"headline": "x"}),
    ("POST", f"/api/admin/newsroom/events/{SOME_ID}/reanalyze", None),
    ("POST", "/api/admin/newsroom/events/merge", {"target_id": SOME_ID, "source_ids": []}),
    ("POST", f"/api/admin/newsroom/events/{SOME_ID}/split", {"article_ids": [SOME_ID]}),
    ("PUT", f"/api/admin/newsroom/articles/{SOME_ID}/body", {"body": "x"}),
    ("POST", "/api/admin/newsroom/articles/manual", {"url": "https://a.example/x"}),
]


class _UnusedDatabase:
    """Fails loudly if a request that should be rejected reaches a query."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"database used before authorization: {name}")


def _client(role: SystemRole) -> AsyncClient:
    # Never connected: every request here is rejected before a query runs.
    engine = create_async_engine("postgresql+psycopg://unused:unused@127.0.0.1:1/unused")
    app = create_app(
        Settings(environment="test"), readiness(True), session_factory=async_sessionmaker(engine)
    )
    user = User(
        id=uuid.uuid4(),
        email="someone@example.com",
        display_name="Someone",
        password_hash="unused",
        must_change_password=False,
        system_role=role,
        status=UserStatus.ACTIVE,
    )
    session = Session(csrf_token_hash=hash_token(CSRF_TOKEN, LOCAL_SESSION_SECRET))

    async def authenticated() -> AuthContext:
        return AuthContext(user=user, session=session, organization_id=None)

    async def no_database() -> AsyncIterator[Any]:
        yield _UnusedDatabase()

    app.dependency_overrides[get_auth_context] = authenticated
    app.dependency_overrides[get_database_session] = no_database
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("role", [SystemRole.ASSET_MANAGER, SystemRole.ORG_MEMBER])
@pytest.mark.parametrize("path", READS)
async def test_reads_require_the_admin_role(role: SystemRole, path: str) -> None:
    async with _client(role) as client:
        response = await client.get(path)
    assert response.status_code == 403


@pytest.mark.parametrize("role", [SystemRole.ASSET_MANAGER, SystemRole.ORG_MEMBER])
@pytest.mark.parametrize(("method", "path", "body"), WRITES)
async def test_writes_require_the_admin_role(
    role: SystemRole, method: str, path: str, body: dict[str, Any] | None
) -> None:
    async with _client(role) as client:
        response = await client.request(
            method, path, json=body, headers={"X-CSRF-Token": CSRF_TOKEN}
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient permission"


@pytest.mark.parametrize(("method", "path", "body"), WRITES)
async def test_writes_require_a_valid_csrf_token(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    async with _client(SystemRole.ADMIN) as client:
        missing = await client.request(method, path, json=body)
        wrong = await client.request(method, path, json=body, headers={"X-CSRF-Token": "nope"})
    assert missing.status_code == 403
    assert missing.json()["detail"] == "CSRF token required"
    assert wrong.status_code == 403
    assert wrong.json()["detail"] == "invalid CSRF token"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/admin/newsroom/sources", {"key": "Bad Key", "name": "x", "kind": "rss"}),
        (
            "POST",
            "/api/admin/newsroom/sources",
            {"key": "manual-2", "name": "x", "kind": "manual", "hostname": "a.example"},
        ),
        (
            "POST",
            "/api/admin/newsroom/sources",
            {"key": "ok", "name": "x", "kind": "rss", "hostname": "a.example", "weight": 3},
        ),
        (
            "POST",
            "/api/admin/newsroom/sources",
            {"key": "ok", "name": "x", "kind": "rss", "hostname": "a.example", "link_pattern": "("},
        ),
        (
            "POST",
            "/api/admin/newsroom/sources",
            {
                "key": "ok",
                "name": "x",
                "kind": "rss",
                "hostname": "a.example",
                "markets": ["global", "global"],
            },
        ),
        ("PUT", f"/api/admin/newsroom/editions/{SOME_ID}/order", {"item_ids": []}),
        ("PUT", f"/api/admin/newsroom/editions/{SOME_ID}/order", {"item_ids": [SOME_ID] * 2}),
        ("PUT", f"/api/admin/newsroom/items/{SOME_ID}/why", {"why": "   "}),
        ("PUT", f"/api/admin/newsroom/items/{SOME_ID}/why", {"why": "x" * 2_001}),
        ("PATCH", f"/api/admin/newsroom/events/{SOME_ID}", {"headline": "x" * 501}),
        (
            "PATCH",
            f"/api/admin/newsroom/events/{SOME_ID}",
            {"related_symbols": [{"symbol": "2330", "kind": "stock", "label": "TSMC"}]},
        ),
        ("POST", "/api/admin/newsroom/events/merge", {"target_id": SOME_ID, "source_ids": []}),
        ("PUT", f"/api/admin/newsroom/articles/{SOME_ID}/body", {"body": "x" * 40_001}),
        (
            "POST",
            "/api/admin/newsroom/articles/manual",
            {"url": "ftp://a.example/x", "edition_date": "2026-10-01"},
        ),
        ("POST", "/api/admin/newsroom/articles/manual", {"url": "https://a.example/x"}),
        ("GET", "/api/admin/newsroom/editions/2026-10-01/asia", None),
    ],
)
async def test_invalid_requests_are_rejected_before_touching_the_database(
    method: str, path: str, body: dict[str, Any] | None
) -> None:
    async with _client(SystemRole.ADMIN) as client:
        response = await client.request(
            method, path, json=body, headers={"X-CSRF-Token": CSRF_TOKEN}
        )
    assert response.status_code == 422
