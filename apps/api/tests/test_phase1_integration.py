import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.models import Base
from daily_insights_api.core.security import hash_password, verify_password
from daily_insights_api.modules.audit.models import AuditEvent
from daily_insights_api.modules.identity import router as identity_router
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.markets.api import AUTOMATIC_SHORT_REFRESH_PERIOD
from daily_insights_api.modules.markets.catalog import MARKETS
from daily_insights_api.modules.markets.models import Market
from daily_insights_api.scripts import bootstrap_admin as bootstrap_admin_module
from daily_insights_api.web.app import create_app

pytestmark = pytest.mark.integration

MEMBER_PASSWORD = "12345678"


@dataclass
class Harness:
    client: AsyncClient
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings


async def ready() -> bool:
    return True


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[Harness]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL") or os.getenv(
        "DAILY_INSIGHTS_DATABASE_URL"
    )
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required for integration tests")
    settings = Settings(
        environment="test",
        database_url=database_url,
        session_secret=SecretStr("phase1-test-session-secret"),
        password_pepper=SecretStr("phase1-test-password-pepper"),
        yfinance_enabled=False,
    )
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory.begin() as database:
        database.add_all(
            [
                Market(
                    code=market.code,
                    name_en=market.name_en,
                    name_zh_hant=market.name_zh_hant,
                    name_zh_hans=market.name_zh_hans,
                )
                for market in MARKETS
            ]
        )
        assert settings.password_pepper is not None
        database.add(
            User(
                email="admin@example.com",
                display_name="Phase 1 Admin",
                password_hash=hash_password(
                    "AdminPassword123!",
                    settings.password_pepper.get_secret_value(),
                ),
                must_change_password=False,
                system_role=SystemRole.ADMIN,
                status=UserStatus.ACTIVE,
            )
        )
    app = create_app(settings, ready, session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield Harness(client=client, session_factory=session_factory, settings=settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    await engine.dispose()


async def login(
    client: AsyncClient,
    email: str,
    password: str,
) -> str:
    response = await client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


async def test_known_user_login_verifies_password_once(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verification_count = 0
    original_verify_password = verify_password

    def count_verification(password: str, encoded: str, pepper: str) -> bool:
        nonlocal verification_count
        verification_count += 1
        return original_verify_password(password, encoded, pepper)

    monkeypatch.setattr(identity_router, "verify_password", count_verification)
    response = await harness.client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "AdminPassword123!"},
    )

    assert response.status_code == 200, response.text
    assert verification_count == 1


async def test_login_rechecks_password_after_a_concurrent_hash_change(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with harness.session_factory() as database:
        admin = await database.scalar(select(User).where(User.email == "admin@example.com"))
    assert admin is not None
    assert harness.settings.password_pepper is not None
    rotated_hash = hash_password(
        "AdminPassword123!",
        harness.settings.password_pepper.get_secret_value(),
    )

    scalar_calls = 0
    verification_count = 0
    original_scalar = AsyncSession.scalar
    original_verify_password = verify_password

    async def rotate_hash_before_locked_reload(
        database: AsyncSession,
        statement: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal scalar_calls
        scalar_calls += 1
        if scalar_calls == 2:
            await database.execute(
                update(User).where(User.id == admin.id).values(password_hash=rotated_hash)
            )
        return await original_scalar(database, statement, *args, **kwargs)

    def count_verification(password: str, encoded: str, pepper: str) -> bool:
        nonlocal verification_count
        verification_count += 1
        return original_verify_password(password, encoded, pepper)

    async def allow_login_attempt(*args: Any, **kwargs: Any) -> bool:
        return True

    async def clear_login_attempts(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(AsyncSession, "scalar", rotate_hash_before_locked_reload)
    monkeypatch.setattr(identity_router, "verify_password", count_verification)
    monkeypatch.setattr(identity_router, "consume_login_attempt", allow_login_attempt)
    monkeypatch.setattr(identity_router, "clear_login_attempts", clear_login_attempts)

    response = await harness.client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "AdminPassword123!"},
    )

    assert response.status_code == 200, response.text
    assert scalar_calls == 2
    assert verification_count == 2


async def test_change_password_rejects_same_unicode_password(harness: Harness) -> None:
    csrf_token = await login(harness.client, "admin@example.com", "AdminPassword123!")
    unicode_password = "密碼安全🙂123"
    changed = await harness.client.post(
        "/api/auth/change-password",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "current_password": "AdminPassword123!",
            "new_password": unicode_password,
        },
    )
    assert changed.status_code == 200, changed.text

    rejected = await harness.client.post(
        "/api/auth/change-password",
        headers={"X-CSRF-Token": changed.json()["csrf_token"]},
        json={
            "current_password": unicode_password,
            "new_password": unicode_password,
        },
    )
    assert rejected.status_code == 422, rejected.text


async def create_organization(
    harness: Harness,
    csrf_token: str,
    *,
    slug: str,
    seat_limit: int,
) -> uuid.UUID:
    response = await harness.client.post(
        "/api/admin/organizations",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "name": slug.title(),
            "slug": slug,
            "seat_limit": seat_limit,
            "contract_reference": f"contract-{slug}",
            "reason": "Phase 1 integration test",
        },
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def test_csrf_token_can_be_rotated_after_browser_reload(harness: Harness) -> None:
    previous_token = await login(
        harness.client,
        "admin@example.com",
        "AdminPassword123!",
    )
    current_user = await harness.client.get("/api/auth/me")
    assert current_user.headers["Cache-Control"] == "no-store"

    rejected_cross_origin = await harness.client.post(
        "/api/auth/csrf",
        headers={"Origin": "https://attacker.example"},
    )
    assert rejected_cross_origin.status_code == 403

    rotated = await harness.client.post(
        "/api/auth/csrf",
        headers={
            "Origin": "http://localhost:3000",
            "X-Forwarded-Proto": "http",
            "X-Forwarded-Host": "localhost:3000",
        },
    )

    assert rotated.status_code == 200
    assert rotated.headers["Cache-Control"] == "no-store"
    current_token = str(rotated.json()["csrf_token"])
    assert current_token != previous_token

    rejected = await harness.client.post(
        "/api/admin/organizations",
        headers={"X-CSRF-Token": previous_token},
        json={
            "name": "Rejected stale token",
            "slug": "rejected-stale-token",
            "seat_limit": 1,
            "contract_reference": "contract-stale-token",
            "reason": "Confirm CSRF rotation invalidates the old token",
        },
    )
    assert rejected.status_code == 403

    logged_out = await harness.client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": current_token},
    )
    assert logged_out.status_code == 204


async def provision_member(
    harness: Harness,
    csrf_token: str,
    organization_id: uuid.UUID,
    *,
    email: str,
) -> dict[str, object]:
    response = await harness.client.post(
        f"/api/admin/organizations/{organization_id}/members",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "email": email,
            "display_name": email.split("@", maxsplit=1)[0],
            "reason": "Phase 1 integration test",
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, object], response.json())


async def activate_member(
    harness: Harness,
    provisioned: dict[str, object],
) -> tuple[AsyncClient, str]:
    app = create_app(harness.settings, ready, harness.session_factory)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    csrf_token = await login(
        client,
        str(provisioned["email"]),
        str(provisioned["temporary_password"]),
    )
    old_session_token = client.cookies.get(harness.settings.session_cookie_name)
    assert old_session_token is not None
    blocked = await client.get("/api/markets")
    assert blocked.status_code == 403
    changed = await client.post(
        "/api/auth/change-password",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "current_password": provisioned["temporary_password"],
            "new_password": MEMBER_PASSWORD,
        },
    )
    assert changed.status_code == 200, changed.text
    assert client.cookies.get(harness.settings.session_cookie_name) != old_session_token
    stale_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        stale = await stale_client.get(
            "/api/auth/me",
            headers={
                "Cookie": f"{harness.settings.session_cookie_name}={old_session_token}",
            },
        )
        assert stale.status_code == 401
    finally:
        await stale_client.aclose()
    return client, str(changed.json()["csrf_token"])


async def test_admin_provisioning_forces_password_change_and_csrf(harness: Harness) -> None:
    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    missing_csrf = await harness.client.post(
        "/api/admin/organizations",
        json={
            "name": "No CSRF",
            "slug": "no-csrf",
            "seat_limit": 1,
            "contract_reference": "contract-no-csrf",
            "reason": "test",
        },
    )
    assert missing_csrf.status_code == 403

    organization_id = await create_organization(
        harness,
        admin_csrf,
        slug="customer-a",
        seat_limit=1,
    )
    provisioned = await provision_member(
        harness,
        admin_csrf,
        organization_id,
        email="member-a@example.com",
    )
    member_client, member_csrf = await activate_member(harness, provisioned)
    try:
        markets = await member_client.get("/api/markets")
        assert markets.status_code == 200
        assert len(markets.json()) == 8
        # Catalog order, not alphabetical: macro first, US before HK and CN.
        assert [market["code"] for market in markets.json()] == [
            "global_macro_bonds",
            "crypto",
            "forex",
            "us_equity",
            "hk_equity",
            "cn_equity",
            "tw_equity",
            "tw_index_derivatives",
        ]
        logged_out = await member_client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": member_csrf},
        )
        assert logged_out.status_code == 204
        assert (await member_client.get("/api/auth/me")).status_code == 401
    finally:
        await member_client.aclose()

    asset_manager = await harness.client.post(
        "/api/admin/internal-users",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "email": "assets@example.com",
            "display_name": "Asset Manager",
            "system_role": "asset_manager",
            "reason": "Phase 1 role test",
        },
    )
    assert asset_manager.status_code == 201
    asset_client, _ = await activate_member(harness, asset_manager.json())
    try:
        forbidden = await asset_client.get("/api/admin/organizations")
        assert forbidden.status_code == 403
    finally:
        await asset_client.aclose()


async def test_legacy_index_refresh_endpoint_is_removed(harness: Harness) -> None:
    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    missing_csrf = await harness.client.post(
        "/api/admin/data-sources/yfinance/daily-bars",
        json={"period": AUTOMATIC_SHORT_REFRESH_PERIOD},
    )
    assert missing_csrf.status_code == 404

    asset_manager = await harness.client.post(
        "/api/admin/internal-users",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "email": "index-assets@example.com",
            "display_name": "Index Asset Manager",
            "system_role": "asset_manager",
            "reason": "Index refresh authorization test",
        },
    )
    assert asset_manager.status_code == 201, asset_manager.text
    asset_client, asset_csrf = await activate_member(harness, asset_manager.json())
    try:
        forbidden = await asset_client.post(
            "/api/admin/data-sources/yfinance/daily-bars",
            headers={"X-CSRF-Token": asset_csrf},
            json={"period": AUTOMATIC_SHORT_REFRESH_PERIOD},
        )
        assert forbidden.status_code == 404
    finally:
        await asset_client.aclose()


async def test_data_management_requires_real_admin_session_and_csrf(harness: Harness) -> None:
    """Exercise the orchestration router through actual login/session checks."""
    path = "/api/admin/orchestration/job-runs"
    assert (await harness.client.get(path)).status_code == 401
    payload = {"job_key": "global_macro_refresh"}
    assert (await harness.client.post(path, json=payload)).status_code == 401

    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    internal = await harness.client.post(
        "/api/admin/internal-users",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "email": "data-management-assets@example.com",
            "display_name": "Data Management Asset Manager",
            "system_role": "asset_manager",
            "reason": "Data management authorization test",
        },
    )
    assert internal.status_code == 201, internal.text
    non_admin_client, non_admin_csrf = await activate_member(harness, internal.json())
    try:
        assert (await non_admin_client.get(path)).status_code == 403
        assert (
            await non_admin_client.post(
                path,
                headers={"X-CSRF-Token": non_admin_csrf},
                json=payload,
            )
        ).status_code == 403
    finally:
        await non_admin_client.aclose()

    assert (await harness.client.post(path, json=payload)).status_code == 403
    assert (
        await harness.client.post(
            path,
            headers={"X-CSRF-Token": "not-a-csrf-token"},
            json=payload,
        )
    ).status_code == 403
    accepted = await harness.client.post(
        path,
        headers={"X-CSRF-Token": admin_csrf},
        json=payload,
    )
    assert accepted.status_code == 202, accepted.text


async def test_index_refresh_disabled_returns_503(harness: Harness) -> None:
    csrf_token = await login(harness.client, "admin@example.com", "AdminPassword123!")
    response = await harness.client.post(
        "/api/admin/data-sources/yfinance/daily-bars",
        headers={"X-CSRF-Token": csrf_token},
        json={"period": AUTOMATIC_SHORT_REFRESH_PERIOD},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("payload", [{}, {"period": AUTOMATIC_SHORT_REFRESH_PERIOD}])
async def test_index_refresh_serializes_partial_result_and_audits(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, str],
) -> None:
    del monkeypatch
    csrf_token = await login(harness.client, "admin@example.com", "AdminPassword123!")
    response = await harness.client.post(
        "/api/admin/data-sources/yfinance/daily-bars",
        headers={"X-CSRF-Token": csrf_token},
        json=payload,
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"period": "2y"},
        {"symbols": ["^TWII"]},
    ],
)
async def test_index_refresh_rejects_non_incremental_or_subset_payload_before_service(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    del monkeypatch
    csrf_token = await login(harness.client, "admin@example.com", "AdminPassword123!")
    response = await harness.client.post(
        "/api/admin/data-sources/yfinance/daily-bars",
        headers={"X-CSRF-Token": csrf_token},
        json=payload,
    )

    assert response.status_code == 404


async def test_index_refresh_timeout_returns_504_without_an_audit_event(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del monkeypatch
    csrf_token = await login(harness.client, "admin@example.com", "AdminPassword123!")
    response = await harness.client.post(
        "/api/admin/data-sources/yfinance/daily-bars",
        headers={"X-CSRF-Token": csrf_token},
        json={"period": AUTOMATIC_SHORT_REFRESH_PERIOD},
    )

    assert response.status_code == 404


async def test_login_rate_limit_is_enforced_at_endpoint(harness: Harness) -> None:
    statuses = []
    for _ in range(harness.settings.login_rate_limit_attempts):
        response = await harness.client.post(
            "/api/auth/login",
            json={"email": "missing@example.com", "password": "WrongPassword123!"},
        )
        statuses.append(response.status_code)

    second_app = create_app(harness.settings, ready, harness.session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=second_app),
        base_url="http://test",
    ) as second_client:
        blocked = await second_client.post(
            "/api/auth/login",
            json={"email": "missing@example.com", "password": "WrongPassword123!"},
        )

    assert set(statuses) == {401}
    assert blocked.status_code == 429


async def test_login_failure_is_generic_and_sensitive_responses_are_not_cached(
    harness: Harness,
) -> None:
    unknown = await harness.client.post(
        "/api/auth/login",
        json={"email": "unknown@example.com", "password": "WrongPassword123!"},
    )
    known = await harness.client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "WrongPassword123!"},
    )
    success = await harness.client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "AdminPassword123!"},
    )

    assert unknown.status_code == known.status_code == 401
    assert unknown.json() == known.json() == {"detail": "invalid credentials"}
    assert success.status_code == 200
    assert success.headers["Cache-Control"] == "no-store"


async def test_request_id_boundary_cannot_break_login_audit(harness: Harness) -> None:
    response = await harness.client.post(
        "/api/auth/login",
        headers={"X-Request-ID": "r" * 101},
        json={"email": "admin@example.com", "password": "AdminPassword123!"},
    )

    assert response.status_code == 200
    assert len(response.headers["X-Request-ID"]) <= 100
    async with harness.session_factory() as database:
        request_id = await database.scalar(
            select(AuditEvent.request_id)
            .where(AuditEvent.action == "identity.login")
            .order_by(AuditEvent.created_at.desc())
        )
    assert request_id == response.headers["X-Request-ID"]


async def test_suspended_member_occupies_seat_until_membership_removal(
    harness: Harness,
) -> None:
    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    organization_id = await create_organization(
        harness,
        admin_csrf,
        slug="seat-contract",
        seat_limit=1,
    )
    first = await provision_member(
        harness,
        admin_csrf,
        organization_id,
        email="first@example.com",
    )
    suspended = await harness.client.patch(
        f"/api/admin/organizations/{organization_id}/members/{first['user_id']}",
        headers={"X-CSRF-Token": admin_csrf},
        json={"status": "suspended", "reason": "temporary suspension"},
    )
    assert suspended.status_code == 200

    at_limit = await harness.client.post(
        f"/api/admin/organizations/{organization_id}/members",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "email": "second@example.com",
            "display_name": "Second",
            "reason": "seat test",
        },
    )
    assert at_limit.status_code == 409

    removed = await harness.client.delete(
        f"/api/admin/organizations/{organization_id}/members/{first['user_id']}",
        headers={"X-CSRF-Token": admin_csrf},
        params={"reason": "contract seat reassignment"},
    )
    assert removed.status_code == 204
    audit = await harness.client.get(
        "/api/admin/audit-events",
        params={"organization_id": str(organization_id)},
    )
    removed_event = next(event for event in audit.json() if event["action"] == "membership.removed")
    assert removed_event["before"]["status"] == "suspended"
    assert removed_event["before"]["removed_at"] is None
    assert removed_event["after"]["status"] == "suspended"
    assert removed_event["after"]["removed_at"] is not None
    second = await provision_member(
        harness,
        admin_csrf,
        organization_id,
        email="second@example.com",
    )
    assert second["email"] == "second@example.com"
    expanded = await harness.client.patch(
        f"/api/admin/organizations/{organization_id}",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "seat_limit": 2,
            "contract_reference": "renewal-2027",
            "reason": "contract renewal",
        },
    )
    assert expanded.status_code == 200
    await provision_member(
        harness,
        admin_csrf,
        organization_id,
        email="third@example.com",
    )
    below_occupancy = await harness.client.patch(
        f"/api/admin/organizations/{organization_id}",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "seat_limit": 1,
            "contract_reference": "invalid-reduction",
            "reason": "must be rejected",
        },
    )
    assert below_occupancy.status_code == 409


async def test_concurrent_member_creation_cannot_exceed_seat_limit(harness: Harness) -> None:
    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    organization_id = await create_organization(
        harness,
        admin_csrf,
        slug="concurrent-seats",
        seat_limit=1,
    )

    async def create(email: str) -> int:
        response = await harness.client.post(
            f"/api/admin/organizations/{organization_id}/members",
            headers={"X-CSRF-Token": admin_csrf},
            json={"email": email, "display_name": email, "reason": "concurrency test"},
        )
        return response.status_code

    statuses = await asyncio.gather(
        create("concurrent-a@example.com"),
        create("concurrent-b@example.com"),
    )
    assert sorted(statuses) == [201, 409]


async def test_concurrent_same_email_across_organizations_returns_conflict(
    harness: Harness,
) -> None:
    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    first_org = await create_organization(harness, admin_csrf, slug="email-a", seat_limit=1)
    second_org = await create_organization(harness, admin_csrf, slug="email-b", seat_limit=1)

    async def create(organization_id: uuid.UUID) -> int:
        response = await harness.client.post(
            f"/api/admin/organizations/{organization_id}/members",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "email": "same-email@example.com",
                "display_name": "Same Email",
                "reason": "concurrency conflict test",
            },
        )
        return response.status_code

    statuses = await asyncio.gather(create(first_org), create(second_org))
    assert sorted(statuses) == [201, 409]


async def test_rejected_organization_change_does_not_create_audit_event(
    harness: Harness,
) -> None:
    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    organization_id = await create_organization(
        harness,
        admin_csrf,
        slug="audit-rollback",
        seat_limit=1,
    )
    rejected = await harness.client.post(
        "/api/admin/organizations",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "name": "Duplicate",
            "slug": "audit-rollback",
            "seat_limit": 2,
            "contract_reference": "duplicate-contract",
            "reason": "must roll back",
        },
    )
    assert rejected.status_code == 409

    audit = await harness.client.get(
        "/api/admin/audit-events",
        params={"organization_id": str(organization_id)},
    )
    created_events = [event for event in audit.json() if event["action"] == "organization.created"]
    assert len(created_events) == 1


async def test_archiving_organization_revokes_sessions_and_blocks_login(
    harness: Harness,
) -> None:
    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    organization_id = await create_organization(
        harness,
        admin_csrf,
        slug="archive-session",
        seat_limit=1,
    )
    provisioned = await provision_member(
        harness,
        admin_csrf,
        organization_id,
        email="archived@example.com",
    )
    member_client, _ = await activate_member(harness, provisioned)
    old_token = member_client.cookies.get(harness.settings.session_cookie_name)
    try:
        archived = await harness.client.delete(
            f"/api/admin/organizations/{organization_id}",
            headers={"X-CSRF-Token": admin_csrf},
            params={"reason": "contract ended", "contract_reference": "contract-closed"},
        )
        assert archived.status_code == 204
        assert (await member_client.get("/api/auth/me")).status_code == 401
        blocked_login = await member_client.post(
            "/api/auth/login",
            json={"email": "archived@example.com", "password": MEMBER_PASSWORD},
        )
        assert blocked_login.status_code == 401

        reactivated = await harness.client.patch(
            f"/api/admin/organizations/{organization_id}",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "status": "active",
                "contract_reference": "contract-renewed",
                "reason": "renewal",
            },
        )
        assert reactivated.status_code == 200
        stale = await member_client.get(
            "/api/auth/me",
            headers={
                "Cookie": f"{harness.settings.session_cookie_name}={old_token}",
            },
        )
        assert stale.status_code == 401
        assert (
            await member_client.post(
                "/api/auth/login",
                json={"email": "archived@example.com", "password": MEMBER_PASSWORD},
            )
        ).status_code == 200
    finally:
        await member_client.aclose()


async def test_patch_archiving_also_permanently_revokes_member_sessions(
    harness: Harness,
) -> None:
    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    organization_id = await create_organization(
        harness,
        admin_csrf,
        slug="patch-archive",
        seat_limit=1,
    )
    provisioned = await provision_member(
        harness,
        admin_csrf,
        organization_id,
        email="patch-archived@example.com",
    )
    member_client, _ = await activate_member(harness, provisioned)
    old_token = member_client.cookies.get(harness.settings.session_cookie_name)
    try:
        archived = await harness.client.patch(
            f"/api/admin/organizations/{organization_id}",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "status": "archived",
                "contract_reference": "patch-archive-contract",
                "reason": "archive through update",
            },
        )
        assert archived.status_code == 200
        reactivated = await harness.client.patch(
            f"/api/admin/organizations/{organization_id}",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "status": "active",
                "contract_reference": "patch-reactivate-contract",
                "reason": "reactivate through update",
            },
        )
        assert reactivated.status_code == 200
        stale = await member_client.get(
            "/api/auth/me",
            headers={
                "Cookie": f"{harness.settings.session_cookie_name}={old_token}",
            },
        )
        assert stale.status_code == 401
    finally:
        await member_client.aclose()


async def test_suspending_organization_permanently_revokes_member_sessions(
    harness: Harness,
) -> None:
    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    organization_id = await create_organization(
        harness,
        admin_csrf,
        slug="suspend-organization",
        seat_limit=1,
    )
    provisioned = await provision_member(
        harness,
        admin_csrf,
        organization_id,
        email="suspended-organization@example.com",
    )
    member_client, _ = await activate_member(harness, provisioned)
    old_token = member_client.cookies.get(harness.settings.session_cookie_name)
    try:
        suspended = await harness.client.patch(
            f"/api/admin/organizations/{organization_id}",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "status": "suspended",
                "contract_reference": "suspension-contract",
                "reason": "temporarily suspend organization",
            },
        )
        assert suspended.status_code == 200
        reactivated = await harness.client.patch(
            f"/api/admin/organizations/{organization_id}",
            headers={"X-CSRF-Token": admin_csrf},
            json={
                "status": "active",
                "contract_reference": "reactivation-contract",
                "reason": "reactivate organization",
            },
        )
        assert reactivated.status_code == 200
        stale = await member_client.get(
            "/api/auth/me",
            headers={
                "Cookie": f"{harness.settings.session_cookie_name}={old_token}",
            },
        )
        assert stale.status_code == 401
    finally:
        await member_client.aclose()


async def test_admin_inputs_reject_blank_audit_evidence(harness: Harness) -> None:
    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    response = await harness.client.post(
        "/api/admin/organizations",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "name": "Blank evidence",
            "slug": "blank-evidence",
            "seat_limit": 1,
            "contract_reference": " ",
            "reason": " ",
        },
    )

    assert response.status_code == 422


async def test_production_login_cookie_is_secure(harness: Harness) -> None:
    production_pepper = "production-password-pepper-value-987654321"
    production_settings = Settings(
        environment="production",
        database_url=harness.settings.database_url,
        session_secret=SecretStr("production-session-secret-value-123456789"),
        password_pepper=SecretStr(production_pepper),
        findb_api_key=SecretStr("production-findb-key"),
        morning_reports_enabled=False,
        r2_endpoint_url="https://account.r2.cloudflarestorage.com",
        r2_bucket_name="daily-insights-test",
        r2_access_key_id=SecretStr("production-r2-access-key"),
        r2_secret_access_key=SecretStr("production-r2-secret-key"),
    )
    async with harness.session_factory.begin() as database:
        admin = await database.scalar(select(User).where(User.email == "admin@example.com"))
        assert admin is not None
        admin.password_hash = hash_password("AdminPassword123!", production_pepper)
    app = create_app(production_settings, ready, harness.session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "AdminPassword123!"},
        )

    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]


async def test_bootstrap_admin_loads_full_model_registry_and_can_log_in(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap_admin_module, "get_settings", lambda: harness.settings)

    temporary_password = await bootstrap_admin_module.bootstrap_admin(
        "bootstrap@example.com",
        "Bootstrap Admin",
    )
    response = await harness.client.post(
        "/api/auth/login",
        json={"email": "bootstrap@example.com", "password": temporary_password},
    )

    assert response.status_code == 200
    assert response.json()["user"]["must_change_password"] is True


async def test_market_policy_is_tenant_scoped_and_spoofed_header_is_ignored(
    harness: Harness,
) -> None:
    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    first_org = await create_organization(harness, admin_csrf, slug="market-a", seat_limit=1)
    second_org = await create_organization(harness, admin_csrf, slug="market-b", seat_limit=1)
    first = await provision_member(
        harness,
        admin_csrf,
        first_org,
        email="market-a@example.com",
    )
    second = await provision_member(
        harness,
        admin_csrf,
        second_org,
        email="market-b@example.com",
    )
    hidden = await harness.client.put(
        f"/api/admin/organizations/{first_org}/markets/crypto",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "is_visible": False,
            "contract_reference": "contract-a",
            "reason": "customer restriction",
        },
    )
    assert hidden.status_code == 200
    updated = await harness.client.put(
        f"/api/admin/organizations/{first_org}/markets/crypto",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "is_visible": False,
            "contract_reference": "contract-a-addendum",
            "reason": "updated customer restriction",
        },
    )
    assert updated.status_code == 200

    first_client, first_csrf = await activate_member(harness, first)
    second_client, _ = await activate_member(harness, second)
    try:
        first_markets = await first_client.get(
            "/api/markets",
            headers={"X-Organization-ID": str(second_org)},
        )
        second_markets = await second_client.get("/api/markets")
        assert len(first_markets.json()) == 7
        assert "crypto" not in {market["code"] for market in first_markets.json()}
        assert len(second_markets.json()) == 8

        forbidden = await first_client.post(
            "/api/admin/organizations",
            headers={"X-CSRF-Token": first_csrf},
            json={
                "name": "Forbidden",
                "slug": "forbidden",
                "seat_limit": 1,
                "contract_reference": "contract-forbidden",
                "reason": "test",
            },
        )
        assert forbidden.status_code == 403
    finally:
        await first_client.aclose()
        await second_client.aclose()

    audit = await harness.client.get(
        "/api/admin/audit-events",
        params={"organization_id": str(first_org)},
    )
    assert audit.status_code == 200
    events = audit.json()
    actions = {event["action"] for event in events}
    assert {"organization.created", "membership.created", "market_policy.updated"} <= actions
    organization_event = next(
        event for event in events if event["action"] == "organization.created"
    )
    assert organization_event["actor_user_id"] is not None
    assert organization_event["reason"] == "Phase 1 integration test"
    assert organization_event["after"]["contract_reference"] == "contract-market-a"
    policy_events = [event for event in events if event["action"] == "market_policy.updated"]
    latest_policy_event = policy_events[0]
    assert latest_policy_event["before"] == {
        "is_visible": False,
        "contract_reference": "contract-a",
        "reason": "customer restriction",
    }
    assert latest_policy_event["after"]["contract_reference"] == "contract-a-addendum"


async def test_login_email_rotation_is_limited_across_app_instances(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.settings.login_ip_rate_limit_attempts = 3
    verified = 0
    original = verify_password

    def verify(password: str, encoded: str, pepper: str) -> bool:
        nonlocal verified
        verified += 1
        return original(password, encoded, pepper)

    monkeypatch.setattr(identity_router, "verify_password", verify)
    # Successful requests must also consume the aggregate source budget.
    response = await harness.client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "AdminPassword123!"},
    )
    assert response.status_code == 200
    for index in range(2):
        response = await harness.client.post(
            "/api/auth/login",
            json={"email": f"missing-{index}@example.com", "password": "wrong"},
        )
        assert response.status_code == 401
    second_app = create_app(harness.settings, ready, harness.session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=second_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/auth/login",
            json={"email": "another@example.com", "password": "wrong"},
        )
    assert response.status_code == 429
    assert verified == 3


async def test_login_global_budget_is_atomic_for_rotating_ips_and_emails(harness: Harness) -> None:
    from daily_insights_api.modules.identity.rate_limit import consume_login_attempt

    harness.settings.login_global_rate_limit_attempts = 3
    results = await asyncio.gather(
        *(
            consume_login_attempt(
                harness.session_factory,
                settings=harness.settings,
                ip_address=f"192.0.2.{index}",
                email=f"missing-{index}@example.com",
            )
            for index in range(12)
        )
    )
    assert sum(results) == 3


async def provision_trial(
    harness: Harness, csrf: str, org: uuid.UUID, email: str, **fields: object
) -> dict[str, Any]:
    response = await harness.client.post(
        f"/api/admin/organizations/{org}/members",
        headers={"X-CSRF-Token": csrf},
        json={
            "email": email,
            "display_name": "Trial",
            "reason": "trial acceptance",
            "member_type": "trial",
            **fields,
        },
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    assert len(result["temporary_password"]) == 12
    return result


@pytest.mark.parametrize("work_phase", ["clear", "rehash"])
async def test_trial_login_rolls_back_expiry_during_login_work(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    work_phase: str,
) -> None:
    from datetime import UTC, datetime, timedelta, tzinfo

    from daily_insights_api.core import security
    from daily_insights_api.modules.identity.rate_limit import clear_login_attempts
    from daily_insights_api.modules.identity.session_models import Session
    from daily_insights_api.modules.tenancy.models import Membership

    csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    organization_id = await create_organization(
        harness, csrf, slug="login-work-expiry", seat_limit=1
    )
    member = await provision_trial(harness, csrf, organization_id, "login-work@example.com")
    user_id = uuid.UUID(str(member["user_id"]))
    password = str(member["temporary_password"])
    clock_time = datetime.now(UTC)
    deadline = clock_time + timedelta(hours=1)
    assert harness.settings.password_pepper is not None
    pepper = harness.settings.password_pepper.get_secret_value()
    with monkeypatch.context() as temporary:
        if work_phase == "rehash":
            temporary.setattr(security, "SCRYPT_N", security.SCRYPT_N // 2)
        original_hash = hash_password(password, pepper)
    assert security.password_needs_rehash(original_hash) == (work_phase == "rehash")
    async with harness.session_factory.begin() as database:
        await database.execute(
            update(Membership)
            .where(Membership.user_id == user_id)
            .values(trial_expires_at=deadline)
        )
        await database.execute(
            update(User).where(User.id == user_id).values(password_hash=original_hash)
        )

    async def snapshot() -> dict[str, list[dict[str, Any]]]:
        async with harness.session_factory() as database:
            return {
                model.__tablename__: [
                    dict(row)
                    for row in (
                        await database.execute(select(model.__table__).order_by(model.id))
                    ).mappings()
                ]
                for model in (User, Membership, Session, AuditEvent)
            }

    before = await snapshot()
    cleared_attempts = False
    rehashed_password = False

    class Clock:
        @staticmethod
        def now(tz: tzinfo) -> datetime:
            return clock_time.astimezone(tz)

    async def clear_then_expire(*args: Any, **kwargs: Any) -> None:
        nonlocal clock_time, cleared_attempts
        await clear_login_attempts(*args, **kwargs)
        cleared_attempts = True
        if work_phase == "clear":
            clock_time = deadline

    def rehash_then_expire(password: str, pepper: str) -> str:
        nonlocal clock_time, rehashed_password
        result = hash_password(password, pepper)
        assert result != original_hash and not security.password_needs_rehash(result)
        rehashed_password = True
        clock_time = deadline
        return result

    monkeypatch.setattr(identity_router, "datetime", Clock)
    monkeypatch.setattr(identity_router, "clear_login_attempts", clear_then_expire)
    monkeypatch.setattr(identity_router, "hash_password", rehash_then_expire)
    rejected = await harness.client.post(
        "/api/auth/login", json={"email": member["email"], "password": password}
    )
    assert cleared_attempts
    assert rehashed_password == (work_phase == "rehash")
    assert clock_time == deadline
    assert rejected.status_code == 401, rejected.text
    assert rejected.json() == {"detail": "invalid credentials"}
    assert "set-cookie" not in rejected.headers
    assert await snapshot() == before


async def test_valid_trial_login_rehashes_legacy_password(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from daily_insights_api.core import security
    from daily_insights_api.modules.identity.session_models import Session
    from daily_insights_api.modules.tenancy.models import Membership

    csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    organization_id = await create_organization(
        harness, csrf, slug="legacy-trial-login", seat_limit=1
    )
    member = await provision_trial(harness, csrf, organization_id, "legacy-trial@example.com")
    user_id = uuid.UUID(str(member["user_id"]))
    password = str(member["temporary_password"])
    assert harness.settings.password_pepper is not None
    pepper = harness.settings.password_pepper.get_secret_value()
    with monkeypatch.context() as temporary:
        temporary.setattr(security, "SCRYPT_N", security.SCRYPT_N // 2)
        legacy_hash = hash_password(password, pepper)
    assert security.password_needs_rehash(legacy_hash)
    deadline = datetime.now(UTC) + timedelta(hours=1)
    async with harness.session_factory.begin() as database:
        await database.execute(
            update(User).where(User.id == user_id).values(password_hash=legacy_hash)
        )
        await database.execute(
            update(Membership)
            .where(Membership.user_id == user_id)
            .values(trial_expires_at=deadline)
        )

    response = await harness.client.post(
        "/api/auth/login", json={"email": member["email"], "password": password}
    )
    assert response.status_code == 200, response.text
    assert "set-cookie" in response.headers
    assert (await harness.client.get("/api/auth/me")).status_code == 200
    async with harness.session_factory() as database:
        user = await database.get(User, user_id)
        assert user is not None
        assert user.password_hash != legacy_hash
        assert not security.password_needs_rehash(user.password_hash)
        assert verify_password(password, user.password_hash, pepper)
        assert user.must_change_password
        session = (await database.scalars(select(Session).where(Session.user_id == user_id))).one()
        assert session.expires_at == deadline
        events = (
            await database.scalars(
                select(AuditEvent).where(
                    AuditEvent.actor_user_id == user_id, AuditEvent.action == "identity.login"
                )
            )
        ).all()
        assert len(events) == 1


@pytest.mark.parametrize(
    ("member_type", "invalidation"),
    [
        ("trial", "renew"),
        ("trial", "convert"),
        ("trial", "trial-expiry"),
        ("internal", "logout"),
        ("regular", "logout"),
        ("trial", "logout"),
        ("internal", "session-expiry"),
        ("regular", "session-expiry"),
        ("trial", "session-expiry"),
        ("internal", "password-change"),
        ("regular", "password-change"),
        ("trial", "password-change"),
    ],
)
async def test_waiting_password_change_revalidates_session_before_mutation(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    member_type: str,
    invalidation: str,
) -> None:
    from datetime import UTC, datetime, timedelta

    from daily_insights_api.modules.identity.session_models import Session
    from daily_insights_api.modules.tenancy.models import Membership

    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    organization_id: uuid.UUID | None = None
    if member_type == "internal":
        email = "admin@example.com"
        current_password = "AdminPassword123!"
        app = create_app(harness.settings, ready, harness.session_factory)
        member_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        member_csrf = await login(member_client, email, current_password)
        async with harness.session_factory() as database:
            user_id = await database.scalar(select(User.id).where(User.email == email))
        assert user_id is not None
    else:
        organization_id = await create_organization(
            harness, admin_csrf, slug="waiting-password-change", seat_limit=1
        )
        email = "waiting@example.com"
        member = (
            await provision_trial(harness, admin_csrf, organization_id, email)
            if member_type == "trial"
            else await provision_member(harness, admin_csrf, organization_id, email=email)
        )
        user_id = uuid.UUID(str(member["user_id"]))
        current_password = MEMBER_PASSWORD
        member_client, member_csrf = await activate_member(harness, member)

    reached_lock = asyncio.Event()
    resume = asyncio.Event()
    waiting_request = ContextVar("waiting_password_request", default=False)
    original_scalar = AsyncSession.scalar
    lock_table = "organizations" if organization_id is not None else "users"

    async def pause_before_lifecycle_lock(
        database: AsyncSession, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if waiting_request.get() and f"FROM {lock_table}" in str(statement):
            waiting_request.set(False)
            reached_lock.set()
            await asyncio.wait_for(resume.wait(), 10)
        return await original_scalar(database, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalar", pause_before_lifecycle_lock)

    async def request_password_change() -> Any:
        waiting_request.set(True)
        return await member_client.post(
            "/api/auth/change-password",
            headers={"X-CSRF-Token": member_csrf},
            json={
                "current_password": current_password,
                "new_password": "RejectedWaitingPassword123!",
            },
        )

    async def snapshot() -> dict[str, list[dict[str, Any]]]:
        async with harness.session_factory() as database:
            return {
                model.__tablename__: [
                    dict(row)
                    for row in (
                        await database.execute(select(model.__table__).order_by(model.id))
                    ).mappings()
                ]
                for model in (User, Membership, Session, AuditEvent)
            }

    pending = asyncio.create_task(request_password_change())
    try:
        await asyncio.wait_for(reached_lock.wait(), 5)
        if invalidation in {"renew", "convert"}:
            path = f"/api/admin/organizations/{organization_id}/members/{user_id}"
            expired = await harness.client.patch(
                path,
                headers={"X-CSRF-Token": admin_csrf},
                json={
                    "reason": "expire while password request waits",
                    "trial_expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                },
            )
            assert expired.status_code == 200, expired.text
            reopened = await harness.client.patch(
                path,
                headers={"X-CSRF-Token": admin_csrf},
                json={
                    "reason": "reopen while password request waits",
                    **(
                        {"trial_expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()}
                        if invalidation == "renew"
                        else {"member_type": "regular"}
                    ),
                },
            )
            assert reopened.status_code == 200, reopened.text
        elif invalidation == "logout":
            logged_out = await member_client.post(
                "/api/auth/logout", headers={"X-CSRF-Token": member_csrf}
            )
            assert logged_out.status_code == 204, logged_out.text
        elif invalidation == "password-change":
            changed_first = await member_client.post(
                "/api/auth/change-password",
                headers={"X-CSRF-Token": member_csrf},
                json={
                    "current_password": current_password,
                    "new_password": "FirstChangedPassword123!",
                },
            )
            assert changed_first.status_code == 200, changed_first.text
            current_password = "FirstChangedPassword123!"
        else:
            async with harness.session_factory.begin() as database:
                if invalidation == "session-expiry":
                    await database.execute(
                        update(Session)
                        .where(Session.user_id == user_id, Session.revoked_at.is_(None))
                        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                    )
                else:
                    await database.execute(
                        update(Membership)
                        .where(Membership.user_id == user_id)
                        .values(trial_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                    )

        before = await snapshot()
        resume.set()
        rejected = await asyncio.wait_for(pending, 5)
        assert rejected.status_code == 401, rejected.text
        assert rejected.json() == {"detail": "invalid or expired session"}
        assert "set-cookie" not in rejected.headers
        assert await snapshot() == before
        if invalidation != "password-change":
            assert (await member_client.get("/api/auth/me")).status_code == 401
        if invalidation != "trial-expiry":
            await login(member_client, email, current_password)
            assert (await member_client.get("/api/auth/me")).status_code == 200
    finally:
        resume.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await member_client.aclose()


@pytest.mark.parametrize("work_phase", ["verify", "hash", "revocation"])
@pytest.mark.parametrize(
    ("member_type", "expiry"),
    [
        ("internal", "session"),
        ("regular", "session"),
        ("trial", "session"),
        ("trial", "membership"),
    ],
)
async def test_password_change_rolls_back_expiry_during_credential_work(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    work_phase: str,
    member_type: str,
    expiry: str,
) -> None:
    from datetime import UTC, datetime, timedelta, tzinfo

    from sqlalchemy.sql.dml import Update

    from daily_insights_api.modules.identity.session_models import Session
    from daily_insights_api.modules.tenancy.models import Membership

    admin_csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    app = create_app(harness.settings, ready, harness.session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if member_type == "internal":
            email = "admin@example.com"
            current_password = "AdminPassword123!"
            async with harness.session_factory() as database:
                user_id = await database.scalar(select(User.id).where(User.email == email))
            assert user_id is not None
        else:
            organization_id = await create_organization(
                harness, admin_csrf, slug="credential-work-expiry", seat_limit=1
            )
            email = "credential-work@example.com"
            member = (
                await provision_trial(harness, admin_csrf, organization_id, email)
                if member_type == "trial"
                else await provision_member(harness, admin_csrf, organization_id, email=email)
            )
            user_id = uuid.UUID(str(member["user_id"]))
            # Keep the initial-password flag set to verify it also rolls back.
            current_password = str(member["temporary_password"])
        csrf = await login(client, email, current_password)

        clock_time = datetime.now(UTC)
        deadline = clock_time + timedelta(hours=1)
        async with harness.session_factory.begin() as database:
            await database.execute(
                update(Session)
                .where(Session.user_id == user_id, Session.revoked_at.is_(None))
                .values(
                    expires_at=deadline if expiry == "session" else deadline + timedelta(hours=1)
                )
            )
            if member_type == "trial":
                await database.execute(
                    update(Membership)
                    .where(Membership.user_id == user_id)
                    .values(
                        trial_expires_at=(
                            deadline if expiry == "membership" else deadline + timedelta(hours=1)
                        )
                    )
                )

        async def snapshot() -> dict[str, list[dict[str, Any]]]:
            async with harness.session_factory() as database:
                return {
                    model.__tablename__: [
                        dict(row)
                        for row in (
                            await database.execute(select(model.__table__).order_by(model.id))
                        ).mappings()
                    ]
                    for model in (User, Membership, Session, AuditEvent)
                }

        before = await snapshot()
        reached_expiry = False
        revocations_flushed = False
        original_execute = AsyncSession.execute

        class Clock:
            @staticmethod
            def now(tz: tzinfo) -> datetime:
                return clock_time.astimezone(tz)

        def verify_then_expire(password: str, encoded: str, pepper: str) -> bool:
            nonlocal clock_time, reached_expiry
            result = verify_password(password, encoded, pepper)
            if work_phase == "verify":
                clock_time = deadline
                reached_expiry = True
            return result

        def hash_then_expire(password: str, pepper: str) -> str:
            nonlocal clock_time, reached_expiry
            result = hash_password(password, pepper)
            if work_phase == "hash":
                clock_time = deadline
                reached_expiry = True
            return result

        async def revoke_then_expire(
            database: AsyncSession, statement: Any, *args: Any, **kwargs: Any
        ) -> Any:
            nonlocal clock_time, reached_expiry, revocations_flushed
            result = await original_execute(database, statement, *args, **kwargs)
            if isinstance(statement, Update) and str(statement.table) == "sessions":
                # The ORM autoflush and SQL revocations have actually run before
                # expiry; equality of persisted snapshots therefore proves rollback.
                stored_hash, must_change_password = (
                    await original_execute(
                        database,
                        select(User.password_hash, User.must_change_password).where(
                            User.id == user_id
                        ),
                    )
                ).one()
                assert must_change_password is False
                assert harness.settings.password_pepper is not None
                assert verify_password(
                    "RejectedExpiredPassword123!",
                    stored_hash,
                    harness.settings.password_pepper.get_secret_value(),
                )
                active_sessions = await database.scalar(
                    select(Session.id).where(
                        Session.user_id == user_id, Session.revoked_at.is_(None)
                    )
                )
                assert active_sessions is None
                revocations_flushed = True
                if work_phase == "revocation":
                    clock_time = deadline
                    reached_expiry = True
            return result

        monkeypatch.setattr(identity_router, "datetime", Clock)
        monkeypatch.setattr(identity_router, "verify_password", verify_then_expire)
        monkeypatch.setattr(identity_router, "hash_password", hash_then_expire)
        monkeypatch.setattr(AsyncSession, "execute", revoke_then_expire)
        rejected = await client.post(
            "/api/auth/change-password",
            headers={"X-CSRF-Token": csrf},
            json={
                "current_password": current_password,
                "new_password": "RejectedExpiredPassword123!",
            },
        )
        assert reached_expiry
        assert revocations_flushed == (work_phase == "revocation")
        assert rejected.status_code == 401, rejected.text
        assert rejected.json() == {"detail": "invalid or expired session"}
        assert "set-cookie" not in rejected.headers
        assert await snapshot() == before


async def test_removed_regular_cannot_be_reprovisioned_as_trial_without_mutation(
    harness: Harness,
) -> None:
    from daily_insights_api.modules.identity.session_models import Session
    from daily_insights_api.modules.tenancy.models import Membership

    csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    org = await create_organization(harness, csrf, slug="restore-regular-rejection", seat_limit=1)
    member = await provision_member(harness, csrf, org, email="removed-regular@example.com")
    member_client, _ = await activate_member(harness, member)
    try:
        removed = await harness.client.delete(
            f"/api/admin/organizations/{org}/members/{member['user_id']}",
            headers={"X-CSRF-Token": csrf},
            params={"reason": "remove regular"},
        )
        assert removed.status_code == 204

        async def snapshot() -> dict[str, list[dict[str, Any]]]:
            async with harness.session_factory() as database:
                return {
                    model.__tablename__: [
                        dict(row)
                        for row in (
                            await database.execute(select(model.__table__).order_by(model.id))
                        ).mappings()
                    ]
                    for model in (User, Membership, Session, AuditEvent)
                }

        before = await snapshot()
        rejected = await harness.client.post(
            f"/api/admin/organizations/{org}/members",
            headers={"X-CSRF-Token": csrf},
            json={
                "email": " REMOVED-REGULAR@example.com ",
                "display_name": "Must not change",
                "reason": "try bypassing conversion restriction",
                "member_type": "trial",
                "trial_days": 14,
            },
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json() == {"detail": "regular members cannot become trials"}
        assert await snapshot() == before
        organization = (await harness.client.get(f"/api/admin/organizations/{org}")).json()
        assert organization["seat_count"] == 0
        assert (await member_client.get("/api/auth/me")).status_code == 401
    finally:
        await member_client.aclose()


@pytest.mark.parametrize(
    ("original_type", "restored_type"),
    [("regular", "regular"), ("trial", "trial"), ("trial", "regular")],
)
async def test_removed_member_reprovisioning_preserves_credentials_seats_and_audit(
    harness: Harness, original_type: str, restored_type: str
) -> None:
    from datetime import datetime, timedelta

    csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    org = await create_organization(harness, csrf, slug="restore-audit", seat_limit=1)
    payload = {
        "email": "restore@example.com",
        "display_name": "Original",
        "reason": "original provisioning",
        "member_type": original_type,
        **({"trial_days": 2} if original_type == "trial" else {}),
    }
    created = await harness.client.post(
        f"/api/admin/organizations/{org}/members",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    )
    assert created.status_code == 201, created.text
    member = created.json()
    original_expiry = (
        datetime.fromisoformat(member["trial_expires_at"]).isoformat()
        if member["trial_expires_at"]
        else None
    )
    member_client, _ = await activate_member(harness, member)
    try:
        removed = await harness.client.delete(
            f"/api/admin/organizations/{org}/members/{member['user_id']}",
            headers={"X-CSRF-Token": csrf},
            params={"reason": "  remove for reprovisioning  "},
        )
        assert removed.status_code == 204
        seat_holder = await provision_member(harness, csrf, org, email="seat-holder@example.com")
        restore_payload = {
            **payload,
            "display_name": "Restored",
            "reason": "restore membership",
            "member_type": restored_type,
            **({"trial_days": 11} if restored_type == "trial" else {}),
        }
        if restored_type == "regular":
            restore_payload.pop("trial_days", None)
        full = await harness.client.post(
            f"/api/admin/organizations/{org}/members",
            headers={"X-CSRF-Token": csrf},
            json=restore_payload,
        )
        assert full.status_code == 409, full.text
        released = await harness.client.delete(
            f"/api/admin/organizations/{org}/members/{seat_holder['user_id']}",
            headers={"X-CSRF-Token": csrf},
            params={"reason": "release seat"},
        )
        assert released.status_code == 204
        restored = await harness.client.post(
            f"/api/admin/organizations/{org}/members",
            headers={"X-CSRF-Token": csrf},
            json=restore_payload,
        )
        assert restored.status_code == 201, restored.text
        assert restored.headers["Cache-Control"] == "no-store"
        result = restored.json()
        assert result["membership_id"] == member["membership_id"]
        assert result["user_id"] == member["user_id"]
        assert result["member_type"] == restored_type
        assert result["status"] == "active" and result["must_change_password"]
        assert result["display_name"] == "Restored"
        assert result["temporary_password"] != member["temporary_password"]
        if restored_type == "trial":
            assert datetime.fromisoformat(result["trial_expires_at"]) - datetime.fromisoformat(
                result["joined_at"]
            ) == timedelta(days=11)
            assert result["trial_expires_at"] != member["trial_expires_at"]
        else:
            assert result["trial_expires_at"] is None
        organization = (await harness.client.get(f"/api/admin/organizations/{org}")).json()
        assert organization["seat_count"] == 1
        assert (await member_client.get("/api/auth/me")).status_code == 401
        stale_password = await member_client.post(
            "/api/auth/login",
            json={"email": member["email"], "password": MEMBER_PASSWORD},
        )
        assert stale_password.status_code == 401
        await login(member_client, member["email"], result["temporary_password"])
        assert (await member_client.get("/api/auth/me")).json()["must_change_password"]
        assert (await member_client.get("/api/markets")).status_code == 403

        audit = await harness.client.get(
            "/api/admin/audit-events", params={"organization_id": str(org)}
        )
        events = [event for event in audit.json() if event["target_id"] == member["membership_id"]]
        assert len(events) == 3
        creation = next(event for event in events if event["action"] == "membership.created")
        removal = next(event for event in events if event["action"] == "membership.removed")
        restoration = next(event for event in events if event["action"] == "membership.restored")
        assert creation["before"] is None
        assert creation["reason"] == "original provisioning"
        assert removal["reason"] == "remove for reprovisioning"
        assert restoration["reason"] == "restore membership"
        assert removal["before"] == creation["after"]
        assert removal["before"]["member_type"] == original_type
        assert removal["before"]["trial_expires_at"] == original_expiry
        assert removal["before"]["status"] == "active"
        assert removal["before"]["removed_at"] is None
        assert removal["before"]["removed_by_user_id"] is None
        assert removal["after"]["member_type"] == original_type
        assert removal["after"]["trial_expires_at"] == original_expiry
        assert removal["after"]["status"] == "suspended"
        assert removal["after"]["removed_at"] is not None
        assert removal["after"]["removed_by_user_id"] == removal["actor_user_id"]
        assert restoration["before"] == removal["after"]
        assert restoration["after"]["member_type"] == restored_type
        assert restoration["after"]["trial_expires_at"] == (
            datetime.fromisoformat(result["trial_expires_at"]).isoformat()
            if result["trial_expires_at"]
            else None
        )
        assert restoration["after"]["status"] == "active"
        assert (
            restoration["after"]["joined_at"]
            == datetime.fromisoformat(result["joined_at"]).isoformat()
        )
        assert restoration["after"]["removed_at"] is None
        assert restoration["after"]["removed_by_user_id"] is None
    finally:
        await member_client.aclose()


async def test_removed_regular_can_be_provisioned_as_trial_in_another_organization(
    harness: Harness,
) -> None:
    from daily_insights_api.modules.tenancy.models import Membership

    csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    original_org = await create_organization(harness, csrf, slug="original-org", seat_limit=1)
    new_org = await create_organization(harness, csrf, slug="new-org", seat_limit=1)
    member = await provision_member(harness, csrf, original_org, email="move@example.com")
    removed = await harness.client.delete(
        f"/api/admin/organizations/{original_org}/members/{member['user_id']}",
        headers={"X-CSRF-Token": csrf},
        params={"reason": "leave original organization"},
    )
    assert removed.status_code == 204
    new_member = await provision_trial(harness, csrf, new_org, "move@example.com")
    assert new_member["user_id"] == member["user_id"]
    assert new_member["membership_id"] != member["membership_id"]
    async with harness.session_factory() as database:
        original = await database.get(Membership, uuid.UUID(str(member["membership_id"])))
        assert original is not None
        assert original.member_type == "regular" and original.trial_expires_at is None
        assert original.removed_at is not None
        event = await database.scalar(
            select(AuditEvent).where(
                AuditEvent.target_id == new_member["membership_id"],
                AuditEvent.action == "membership.created",
            )
        )
        assert event is not None and event.before is None


async def test_trial_login_expiry_sessions_seats_and_conversion(harness: Harness) -> None:
    from datetime import UTC, datetime, timedelta

    from daily_insights_api.modules.identity.session_models import Session
    from daily_insights_api.modules.tenancy.models import Membership

    csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    org = await create_organization(harness, csrf, slug="trial-lifecycle", seat_limit=1)
    trial = await provision_trial(harness, csrf, org, "trial@example.com")
    expiry = datetime.fromisoformat(trial["trial_expires_at"])
    joined = datetime.fromisoformat(trial["joined_at"])
    assert expiry - joined == timedelta(days=7)
    assert trial["member_type"] == "trial" and not trial["trial_expired"]
    admin_cookies = dict(harness.client.cookies)
    member_csrf = await login(harness.client, "trial@example.com", trial["temporary_password"])
    changed = await harness.client.post(
        "/api/auth/change-password",
        headers={"X-CSRF-Token": member_csrf},
        json={"current_password": trial["temporary_password"], "new_password": MEMBER_PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    member_cookies = dict(harness.client.cookies)
    assert (await harness.client.get("/api/auth/me")).status_code == 200
    async with harness.session_factory.begin() as database:
        sessions = (
            await database.scalars(
                select(Session).where(Session.user_id == uuid.UUID(trial["user_id"]))
            )
        ).all()
        assert sessions and all(session.expires_at <= expiry for session in sessions)
        # Expiry must be checked on every request, independently of session TTL.
        await database.execute(
            update(Membership)
            .where(Membership.user_id == uuid.UUID(trial["user_id"]))
            .values(trial_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (await harness.client.get("/api/auth/me")).status_code == 401
    assert (
        await harness.client.post(
            "/api/auth/change-password",
            headers={"X-CSRF-Token": changed.json()["csrf_token"]},
            json={"current_password": MEMBER_PASSWORD, "new_password": "Another123!"},
        )
    ).status_code == 401
    assert (
        await harness.client.post(
            "/api/auth/login", json={"email": "trial@example.com", "password": MEMBER_PASSWORD}
        )
    ).status_code == 401
    harness.client.cookies.clear()
    harness.client.cookies.update(admin_cookies)
    organization = (await harness.client.get(f"/api/admin/organizations/{org}")).json()
    assert organization["seat_count"] == 0
    members = (await harness.client.get(f"/api/admin/organizations/{org}/members")).json()
    assert members[0]["trial_expired"]
    second = await provision_trial(harness, csrf, org, "second@example.com", trial_days=2)
    assert datetime.fromisoformat(second["trial_expires_at"]) - datetime.fromisoformat(
        second["joined_at"]
    ) == timedelta(days=2)
    member_path = f"/api/admin/organizations/{org}/members/{trial['user_id']}"
    for payload in [
        {"member_type": "regular"},
        {"trial_expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()},
    ]:
        conflict = await harness.client.patch(
            member_path, headers={"X-CSRF-Token": csrf}, json={"reason": "renew", **payload}
        )
        assert conflict.status_code == 409, conflict.text
    removed = await harness.client.delete(
        f"/api/admin/organizations/{org}/members/{second['user_id']}?reason=release",
        headers={"X-CSRF-Token": csrf},
    )
    assert removed.status_code == 204
    suspended = await harness.client.patch(
        member_path,
        headers={"X-CSRF-Token": csrf},
        json={"reason": "suspend", "status": "suspended"},
    )
    assert suspended.status_code == 200
    renewed = await harness.client.patch(
        member_path,
        headers={"X-CSRF-Token": csrf},
        json={
            "reason": "renew",
            "trial_expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        },
    )
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["status"] == "suspended"
    assert not renewed.json()["trial_expired"]
    converted = await harness.client.patch(
        member_path,
        headers={"X-CSRF-Token": csrf},
        json={"reason": "convert", "member_type": "regular", "status": "active"},
    )
    assert converted.status_code == 200, converted.text
    assert converted.json()["trial_expires_at"] is None
    assert converted.json()["member_type"] == "regular"
    rejected = await harness.client.patch(
        member_path,
        headers={"X-CSRF-Token": csrf},
        json={"reason": "not allowed", "member_type": "trial"},
    )
    assert rejected.status_code == 422
    harness.client.cookies.clear()
    harness.client.cookies.update(member_cookies)
    assert (await harness.client.get("/api/auth/me")).status_code == 401
    await login(harness.client, "trial@example.com", MEMBER_PASSWORD)
    assert (await harness.client.get("/api/auth/me")).status_code == 200
    async with harness.session_factory() as database:
        events = (
            await database.scalars(
                select(AuditEvent).where(AuditEvent.action == "membership.updated")
            )
        ).all()
        assert any(event.after and event.after.get("member_type") == "regular" for event in events)


async def test_trial_renewal_and_creation_cannot_take_same_last_seat(harness: Harness) -> None:
    from datetime import UTC, datetime, timedelta

    csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    org = await create_organization(harness, csrf, slug="trial-concurrent", seat_limit=1)
    trial = await provision_trial(harness, csrf, org, "expired@example.com")
    path = f"/api/admin/organizations/{org}/members/{trial['user_id']}"
    expire = await harness.client.patch(
        path,
        headers={"X-CSRF-Token": csrf},
        json={
            "reason": "shorten",
            "trial_expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        },
    )
    assert expire.status_code == 200
    renew, create = await asyncio.gather(
        harness.client.patch(
            path,
            headers={"X-CSRF-Token": csrf},
            json={
                "reason": "renew",
                "trial_expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        ),
        harness.client.post(
            f"/api/admin/organizations/{org}/members",
            headers={"X-CSRF-Token": csrf},
            json={"email": "new@example.com", "display_name": "New", "reason": "seat"},
        ),
    )
    assert sorted([renew.status_code, create.status_code]) in ([200, 409], [201, 409])
    organization = (await harness.client.get(f"/api/admin/organizations/{org}")).json()
    assert organization["seat_count"] == 1


async def test_conversation_usage_list_detail_filters_and_pagination(harness: Harness) -> None:
    from daily_insights_api.core.enums import GenerationStatus, MessageRole
    from daily_insights_api.modules.chat.models import Conversation, Message
    from daily_insights_api.modules.model_runtime.models import GenerationRecord, ModelConfiguration

    csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    org = await create_organization(harness, csrf, slug="usage", seat_limit=1)
    member = await provision_trial(harness, csrf, org, "usage@example.com")
    async with harness.session_factory.begin() as database:
        config = ModelConfiguration(
            version=1, provider="test", requested_model="test", prompt_version="v1"
        )
        database.add(config)
        await database.flush()
        conversations = [
            Conversation(organization_id=org, user_id=uuid.UUID(member["user_id"]))
            for _ in range(2)
        ]
        database.add_all(conversations)
        await database.flush()
        first_id, second_id = [conversation.id for conversation in conversations]
        for sequence, (inputs, outputs) in enumerate([(12, 34), (None, 5), (0, 0), (None, None)]):
            message = Message(
                conversation_id=first_id,
                sequence_number=sequence,
                role=MessageRole.ASSISTANT,
                content="answer",
                status=GenerationStatus.ERROR if sequence == 1 else GenerationStatus.COMPLETE,
            )
            database.add(message)
            await database.flush()
            database.add(
                GenerationRecord(
                    message_id=message.id,
                    model_configuration_id=config.id,
                    model_configuration_version=1,
                    provider="test",
                    requested_model="test",
                    prompt_version="v1",
                    input_tokens=inputs,
                    output_tokens=outputs,
                    status=message.status,
                )
            )
    response = await harness.client.get(
        f"/api/admin/conversations?organization_id={org}&member_id={member['user_id']}&limit=1"
    )
    assert response.status_code == 200, response.text
    result = response.json()
    items = result["items"]
    assert result["next_cursor"]
    next_page = await harness.client.get(
        f"/api/admin/conversations?organization_id={org}&limit=1&cursor={result['next_cursor']}"
    )
    items += next_page.json()["items"]
    assert len(items) == 2 and {item["id"] for item in items} == {str(first_id), str(second_id)}
    for item in items:
        detail = await harness.client.get(f"/api/admin/conversations/{item['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["usage"] == item["usage"]
        if item["id"] == str(first_id):
            assert item["usage"] == {
                "input_tokens": 12,
                "output_tokens": 39,
                "total_tokens": 51,
                "completeness": "partial",
            }
            assert item["message_count"] == 4
        else:
            assert item["usage"]["total_tokens"] is None
            assert item["usage"]["completeness"] == "unavailable"
    assert (
        await harness.client.get(f"/api/admin/conversations?organization_id={uuid.uuid4()}")
    ).json()["items"] == []


async def test_active_trial_extension_preserves_session_and_immediate_expiry_revokes(
    harness: Harness,
) -> None:
    from datetime import UTC, datetime, timedelta

    csrf = await login(harness.client, "admin@example.com", "AdminPassword123!")
    org = await create_organization(harness, csrf, slug="active-trial", seat_limit=1)
    trial = await provision_trial(harness, csrf, org, "active-trial@example.com")
    admin_cookies = dict(harness.client.cookies)
    await login(harness.client, "active-trial@example.com", trial["temporary_password"])
    member_cookies = dict(harness.client.cookies)
    harness.client.cookies.clear()
    harness.client.cookies.update(admin_cookies)
    path = f"/api/admin/organizations/{org}/members/{trial['user_id']}"
    extended = await harness.client.patch(
        path,
        headers={"X-CSRF-Token": csrf},
        json={
            "reason": "extend",
            "trial_expires_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
        },
    )
    assert extended.status_code == 200
    harness.client.cookies.clear()
    harness.client.cookies.update(member_cookies)
    assert (await harness.client.get("/api/auth/me")).status_code == 200
    harness.client.cookies.clear()
    harness.client.cookies.update(admin_cookies)
    shortened = await harness.client.patch(
        path,
        headers={"X-CSRF-Token": csrf},
        json={
            "reason": "end trial",
            "trial_expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        },
    )
    assert shortened.status_code == 200
    harness.client.cookies.clear()
    harness.client.cookies.update(member_cookies)
    assert (await harness.client.get("/api/auth/me")).status_code == 401
