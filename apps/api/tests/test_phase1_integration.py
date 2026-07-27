import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import cast

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.models import Base
from daily_insights_api.core.security import hash_password
from daily_insights_api.modules.audit.models import AuditEvent
from daily_insights_api.modules.identity.models import User
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
