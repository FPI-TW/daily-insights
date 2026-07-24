import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import OrganizationStatus, SystemRole, UserStatus
from daily_insights_api.core.models import Base
from daily_insights_api.core.security import hash_password
from daily_insights_api.modules.assets.models import Asset
from daily_insights_api.modules.assets.object_store import ObjectMetadata, ObjectRef
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.podcasts.models import PodcastEpisodeAudioVariant
from daily_insights_api.modules.tenancy.models import Membership, Organization
from daily_insights_api.web.app import create_app

pytestmark = pytest.mark.integration


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[ObjectRef, tuple[bytes, str, str | None]] = {}
        self.signed: list[ObjectRef] = []

    def put(self, ref: ObjectRef, body: bytes, mime_type: str) -> None:
        self.objects[ref] = (body, mime_type, None)

    async def head(self, ref: ObjectRef) -> ObjectMetadata | None:
        value = self.objects.get(ref)
        if value is None:
            return None
        body, mime_type, sha256 = value
        return ObjectMetadata(
            ref=ref,
            size_bytes=len(body),
            mime_type=mime_type,
            sha256=sha256,
        )

    async def copy_if_absent(
        self,
        source: ObjectRef,
        target: ObjectRef,
        *,
        sha256: str,
    ) -> bool:
        if target in self.objects:
            return False
        body, mime_type, _ = self.objects[source]
        assert hashlib.sha256(body).hexdigest() == sha256
        self.objects[target] = (body, mime_type, sha256)
        return True

    def read(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        body = self.objects[ref][0]

        async def chunks() -> AsyncIterator[bytes]:
            yield body

        return chunks()

    async def presign_get(self, ref: ObjectRef, expires_in: timedelta) -> str:
        assert ref in self.objects
        assert expires_in == timedelta(minutes=15)
        self.signed.append(ref)
        return f"https://media.example.invalid/{ref.key}"


@dataclass
class PodcastHarness:
    admin: AsyncClient
    asset_manager: AsyncClient
    customer: AsyncClient
    anonymous: AsyncClient
    session_factory: async_sessionmaker[AsyncSession]
    store: FakeObjectStore


async def ready() -> bool:
    return True


async def _login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


@pytest_asyncio.fixture
async def podcast_harness() -> AsyncIterator[PodcastHarness]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL") or os.getenv(
        "DAILY_INSIGHTS_DATABASE_URL"
    )
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    settings = Settings(
        environment="test",
        database_url=database_url,
        session_secret=SecretStr("phase3-session-secret"),
        password_pepper=SecretStr("phase3-password-pepper"),
        r2_bucket_name="podcast-private",
        r2_signed_url_ttl_seconds=900,
    )
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await connection.run_sync(Base.metadata.create_all)

    assert settings.password_pepper is not None
    pepper = settings.password_pepper.get_secret_value()
    organization_id = uuid.uuid4()
    member_id = uuid.uuid4()
    async with session_factory.begin() as database:
        database.add(
            Organization(
                id=organization_id,
                name="Podcast Customer",
                slug="podcast-customer",
                seat_limit=10,
                status=OrganizationStatus.ACTIVE,
            )
        )
        database.add_all(
            [
                User(
                    email="admin@podcast.test",
                    display_name="Podcast Admin",
                    password_hash=hash_password("AdminPassword123!", pepper),
                    must_change_password=False,
                    system_role=SystemRole.ADMIN,
                    status=UserStatus.ACTIVE,
                ),
                User(
                    email="assets@podcast.test",
                    display_name="Asset Manager",
                    password_hash=hash_password("AssetPassword123!", pepper),
                    must_change_password=False,
                    system_role=SystemRole.ASSET_MANAGER,
                    status=UserStatus.ACTIVE,
                ),
                User(
                    id=member_id,
                    email="member@podcast.test",
                    display_name="Podcast Listener",
                    password_hash=hash_password("MemberPassword123!", pepper),
                    must_change_password=False,
                    system_role=SystemRole.ORG_MEMBER,
                    status=UserStatus.ACTIVE,
                ),
            ]
        )
        await database.flush()
        database.add(Membership(organization_id=organization_id, user_id=member_id))

    store = FakeObjectStore()
    app = create_app(settings, ready, session_factory, store)
    clients = [
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") for _ in range(4)
    ]
    try:
        yield PodcastHarness(
            admin=clients[0],
            asset_manager=clients[1],
            customer=clients[2],
            anonymous=clients[3],
            session_factory=session_factory,
            store=store,
        )
    finally:
        for client in clients:
            await client.aclose()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await engine.dispose()


def _metadata() -> list[dict[str, str]]:
    return [
        {"locale": "zh-TW", "title": "市場晨報", "summary": "繁體摘要"},
        {"locale": "zh-CN", "title": "市场晨报", "summary": "简体摘要"},
        {"locale": "en", "title": "Market Brief", "summary": "English summary"},
    ]


async def test_podcast_publish_play_replace_and_unpublish(
    podcast_harness: PodcastHarness,
) -> None:
    admin_csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    asset_csrf = await _login(
        podcast_harness.asset_manager,
        "assets@podcast.test",
        "AssetPassword123!",
    )
    await _login(
        podcast_harness.customer,
        "member@podcast.test",
        "MemberPassword123!",
    )

    anonymous = await podcast_harness.anonymous.get("/api/podcasts")
    assert anonymous.status_code == 401

    created = await podcast_harness.admin.post(
        "/api/admin/podcasts",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "trading_date": "2026-07-24",
            "metadata": {"values": _metadata()},
            "reason": "建立 Podcast 先行版內容",
        },
    )
    assert created.status_code == 201, created.text
    episode = created.json()
    episode_id = episode["id"]

    blocked_publish = await podcast_harness.admin.post(
        f"/api/admin/podcasts/{episode_id}/publish",
        headers={"X-CSRF-Token": admin_csrf},
        json={"expected_version": 1, "reason": "缺少音檔時不得發布"},
    )
    assert blocked_publish.status_code == 422
    assert blocked_publish.json()["detail"]["code"] == "episode_not_publishable"

    source = ObjectRef(bucket="podcast-private", key="legacy/2026-07-24.mp3")
    podcast_harness.store.put(source, b"first-podcast-audio", "audio/mpeg")
    imported = await podcast_harness.asset_manager.post(
        f"/api/admin/podcasts/{episode_id}/audio-imports",
        headers={"X-CSRF-Token": asset_csrf},
        json={
            "source_bucket": source.bucket,
            "source_key": source.key,
            "locale": "zh-TW",
            "expected_mime_type": "audio/mpeg",
            "reason": "登記手動上傳音檔",
        },
    )
    assert imported.status_code == 200, imported.text
    episode = imported.json()
    assert episode["version"] == 2
    assert episode["audio_variants"][0]["version"] == 1

    asset_manager_cannot_publish = await podcast_harness.asset_manager.post(
        f"/api/admin/podcasts/{episode_id}/publish",
        headers={"X-CSRF-Token": asset_csrf},
        json={"expected_version": 2, "reason": "權限測試"},
    )
    assert asset_manager_cannot_publish.status_code == 403

    published = await podcast_harness.admin.post(
        f"/api/admin/podcasts/{episode_id}/publish",
        headers={"X-CSRF-Token": admin_csrf},
        json={"expected_version": 2, "reason": "內容確認完成"},
    )
    assert published.status_code == 200, published.text
    episode = published.json()
    assert episode["status"] == "published"
    assert episode["version"] == 3

    catalog = await podcast_harness.customer.get("/api/podcasts?locale=en")
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()[0]["title"] == "Market Brief"
    detail = await podcast_harness.customer.get(f"/api/podcasts/{episode_id}?locale=zh-CN")
    assert detail.status_code == 200
    assert detail.json()["summary"] == "简体摘要"

    playback = await podcast_harness.customer.post(
        f"/api/podcasts/{episode_id}/audio-url?locale=en"
    )
    assert playback.status_code == 200, playback.text
    assert playback.json()["requested_locale"] == "en"
    assert playback.json()["resolved_locale"] == "zh-TW"
    first_asset_id = playback.json()["asset_id"]

    replacement_source = ObjectRef(
        bucket="podcast-private",
        key="legacy/2026-07-24-v2.mp3",
    )
    podcast_harness.store.put(replacement_source, b"second-podcast-audio", "audio/mpeg")
    warning = await podcast_harness.asset_manager.post(
        f"/api/admin/podcasts/{episode_id}/audio-imports",
        headers={"X-CSRF-Token": asset_csrf},
        json={
            "source_bucket": replacement_source.bucket,
            "source_key": replacement_source.key,
            "locale": "zh-TW",
            "expected_mime_type": "audio/mpeg",
            "reason": "測試替換警告",
        },
    )
    assert warning.status_code == 409
    assert warning.json()["detail"] == {
        "code": "replacement_confirmation_required",
        "current_version": 1,
    }

    replaced = await podcast_harness.asset_manager.post(
        f"/api/admin/podcasts/{episode_id}/audio-imports",
        headers={"X-CSRF-Token": asset_csrf},
        json={
            "source_bucket": replacement_source.bucket,
            "source_key": replacement_source.key,
            "locale": "zh-TW",
            "expected_mime_type": "audio/mpeg",
            "confirm_replacement": True,
            "expected_current_version": 1,
            "reason": "確認替換音檔",
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["version"] == 4

    async with podcast_harness.session_factory() as database:
        variants = (
            await database.scalars(
                select(PodcastEpisodeAudioVariant)
                .where(PodcastEpisodeAudioVariant.episode_id == uuid.UUID(episode_id))
                .order_by(PodcastEpisodeAudioVariant.version)
            )
        ).all()
        assert [(variant.version, variant.is_active) for variant in variants] == [
            (1, False),
            (2, True),
        ]
        assert len((await database.scalars(select(Asset))).all()) == 2

    replacement_playback = await podcast_harness.customer.post(
        f"/api/podcasts/{episode_id}/audio-url?locale=zh-TW"
    )
    assert replacement_playback.status_code == 200
    assert replacement_playback.json()["asset_id"] != first_asset_id

    unpublished = await podcast_harness.admin.post(
        f"/api/admin/podcasts/{episode_id}/unpublish",
        headers={"X-CSRF-Token": admin_csrf},
        json={"expected_version": 4, "reason": "內容下架"},
    )
    assert unpublished.status_code == 200
    assert unpublished.json()["status"] == "draft"
    assert (await podcast_harness.customer.get("/api/podcasts")).json() == []
    unavailable = await podcast_harness.customer.post(f"/api/podcasts/{episode_id}/audio-url")
    assert unavailable.status_code == 404
