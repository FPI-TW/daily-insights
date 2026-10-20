import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from conftest import remigrate_database, reset_database_schema
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_health import readiness
from test_sector_heatmap import (
    DEFAULT_SETTINGS,
    FakeObjectStore,
    _captured_after,
    _running_attempt,
)

import daily_insights_api.modules.orchestration.functions as orchestration_functions
from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.modules.assets.api import ObjectRef, ObjectStore
from daily_insights_api.modules.data_sources.api import CapturedHeatmap
from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.modules.markets.models import OrganizationMarketPolicy
from daily_insights_api.modules.tenancy.models import Organization
from daily_insights_api.web.app import create_app

pytestmark = pytest.mark.integration

ORGANIZATION_ID = uuid.uuid4()
EDITION = date(2026, 10, 12)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    await asyncio.to_thread(remigrate_database, database_url)
    engine = create_async_engine(database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            await engine.dispose()
        finally:
            await asyncio.to_thread(reset_database_schema, database_url)


class SigningObjectStore(FakeObjectStore):
    async def presign_get(self, ref: ObjectRef, expires_in: timedelta) -> str:
        return f"https://signed.test/{ref.bucket}/{ref.key}?ttl={int(expires_in.total_seconds())}"


def _client(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    object_store: ObjectStore | None,
    role: SystemRole = SystemRole.ORG_MEMBER,
    organization_id: uuid.UUID | None = ORGANIZATION_ID,
    settings: dict[str, Any] | None = None,
) -> AsyncClient:
    values = {**DEFAULT_SETTINGS, "r2_signed_url_ttl_seconds": 600, **(settings or {})}
    if object_store is None:
        # Without credentials the app builds no store of its own either.
        values.update(r2_endpoint_url=None, r2_access_key_id=None, r2_secret_access_key=None)
    app = create_app(
        Settings.model_validate(values), readiness(True), session_factory, object_store
    )

    async def auth() -> AuthContext:
        return AuthContext(
            user=User(system_role=role), session=Session(), organization_id=organization_id
        )

    app.dependency_overrides[require_password_changed] = auth
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed_edition(
    session_factory: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
    monkeypatch: pytest.MonkeyPatch,
    edition_date: date,
    seed: bytes = b"",
) -> None:
    # Captured the morning after the edition's bar, with the worker's clock
    # pinned there too, so neither gate mistakes the seed for a live session.
    captured = _captured_after(edition_date, seed)

    async def capture(**_: object) -> CapturedHeatmap:
        return captured

    monkeypatch.setattr(orchestration_functions, "_utcnow", lambda: captured.captured_at)
    monkeypatch.setattr(orchestration_functions, "capture_sector_heatmap", capture)
    outcome = await orchestration_functions._run_tradingview_heatmap(
        Settings.model_validate(DEFAULT_SETTINGS),
        session_factory,
        # The edition follows the newest settled ^HSI bar, so seed one dated as asked.
        await _running_attempt(session_factory, edition_date, settled=edition_date),
        store,
    )
    assert outcome.status == "succeeded"


async def test_latest_edition_is_returned_with_signed_urls_in_display_order(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SigningObjectStore()
    # The previous session: EDITION is a Monday, so Friday.
    older = EDITION - timedelta(days=3)
    await _seed_edition(session_factory, store, monkeypatch, older)
    await _seed_edition(session_factory, store, monkeypatch, EDITION, b"today")

    async with _client(session_factory, object_store=store) as client:
        latest = await client.get("/api/markets/hk/sector-heatmap")
        dated = await client.get(
            "/api/markets/hk/sector-heatmap", params={"date": older.isoformat()}
        )
        missing = await client.get("/api/markets/hk/sector-heatmap", params={"date": "2020-01-01"})

    assert latest.status_code == 200, latest.text
    body = latest.json()
    assert body["data_source"] == "HSI"
    assert body["edition_date"] == "2026-10-12"
    assert datetime.fromisoformat(body["captured_at"]).tzinfo is not None
    assert [(image["kind"], image["slug"], image["name"]) for image in body["images"]] == [
        ("overview", "overview", "Overview"),
        ("legend", "legend", "Legend"),
    ]
    overview = body["images"][0]
    assert (overview["width"], overview["height"]) == (1920, 989)
    assert overview["expires_in_seconds"] == 600
    assert (
        overview["url"]
        == "https://signed.test/daily-insights-test/heatmaps/hsi/2026-10-12/overview.png?ttl=600"
    )

    assert dated.status_code == 200
    assert dated.json()["edition_date"] == older.isoformat()
    assert [image["slug"] for image in dated.json()["images"]] == ["overview", "legend"]
    assert missing.status_code == 404


async def test_empty_ledger_and_missing_object_store_are_reported(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client(session_factory, object_store=SigningObjectStore()) as client:
        empty = await client.get("/api/markets/hk/sector-heatmap")
    async with _client(session_factory, object_store=None) as client:
        unconfigured = await client.get("/api/markets/hk/sector-heatmap")
    # A development box often sets endpoint and keys but no bucket: the app
    # still builds a store, and a stored edition must answer 503, not 500.
    store = SigningObjectStore()
    await _seed_edition(session_factory, store, monkeypatch, EDITION)
    async with _client(
        session_factory, object_store=store, settings={"r2_bucket_name": None}
    ) as client:
        no_bucket = await client.get("/api/markets/hk/sector-heatmap")

    assert empty.status_code == 404
    assert empty.json()["detail"]["code"] == "sector_heatmap_not_captured"
    assert unconfigured.status_code == 503
    assert no_bucket.status_code == 503
    assert no_bucket.json()["detail"]["code"] == "r2_not_configured"


async def test_heatmap_follows_hong_kong_market_visibility(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SigningObjectStore()
    await _seed_edition(session_factory, store, monkeypatch, EDITION)
    hidden_from = uuid.uuid4()
    author_id = uuid.uuid4()
    async with session_factory.begin() as database:
        database.add_all(
            [
                Organization(id=hidden_from, name="Contract A", slug="contract-a", seat_limit=1),
                User(
                    id=author_id,
                    email="policy-author@example.com",
                    display_name="Policy Author",
                    password_hash="unused",
                    must_change_password=False,
                    system_role=SystemRole.ADMIN,
                    status=UserStatus.ACTIVE,
                ),
            ]
        )
        await database.flush()
        database.add(
            OrganizationMarketPolicy(
                organization_id=hidden_from,
                market_code="hk_equity",
                is_visible=False,
                changed_by_user_id=author_id,
                changed_at=datetime.now(UTC),
            )
        )

    async with _client(session_factory, object_store=store, organization_id=hidden_from) as blocked:
        hidden = await blocked.get("/api/markets/hk/sector-heatmap")
    async with _client(
        session_factory, object_store=store, role=SystemRole.ADMIN, organization_id=None
    ) as staff:
        preview = await staff.get("/api/markets/hk/sector-heatmap")

    assert hidden.status_code == 404
    # A hidden market is indistinguishable from a missing index, not from an
    # empty ledger: the web app must not render it as "no heatmap yet".
    assert hidden.json()["detail"] == "index not found"
    assert preview.status_code == 200, preview.text
    assert len(preview.json()["images"]) == 2
