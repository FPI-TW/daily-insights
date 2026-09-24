import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

import pytest
import pytest_asyncio
from botocore.exceptions import ClientError, EndpointConnectionError
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import AssetKind, OrganizationStatus, SystemRole, UserStatus
from daily_insights_api.core.models import Base
from daily_insights_api.core.security import hash_password
from daily_insights_api.modules.assets.models import Asset
from daily_insights_api.modules.assets.object_store import ObjectMetadata, ObjectRef
from daily_insights_api.modules.audit.models import AuditEvent
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.podcasts.media_worker import (
    MAX_PROCESSING_ATTEMPTS,
    RETRY_BASE_SECONDS,
    PodcastMediaWorker,
)
from daily_insights_api.modules.podcasts.models import PodcastEpisode, PodcastEpisodeAudioVariant
from daily_insights_api.modules.podcasts.upload_models import PodcastUploadSession
from daily_insights_api.modules.tenancy.models import Membership, Organization
from daily_insights_api.web.app import create_app

pytestmark = pytest.mark.integration


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[ObjectRef, tuple[bytes, str, str | None]] = {}
        self.signed: list[ObjectRef] = []
        self.put_signatures: list[tuple[ObjectRef, str, str, timedelta]] = []
        self.read_count = 0
        self.head_errors: dict[ObjectRef, list[Exception]] = {}
        self.read_errors: dict[ObjectRef, list[Exception]] = {}
        self.delete_errors: dict[ObjectRef, list[Exception]] = {}

    def put(
        self,
        ref: ObjectRef,
        body: bytes,
        mime_type: str,
        sha256: str | None = None,
    ) -> None:
        self.objects[ref] = (body, mime_type, sha256)

    async def head(self, ref: ObjectRef) -> ObjectMetadata | None:
        errors = self.head_errors.get(ref)
        if errors:
            raise errors.pop(0)
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

    async def put_if_absent(
        self,
        target: ObjectRef,
        content: BinaryIO,
        *,
        size_bytes: int,
        mime_type: str,
        sha256: str,
    ) -> bool:
        if target in self.objects:
            return False
        body = content.read()
        assert len(body) == size_bytes
        assert hashlib.sha256(body).hexdigest() == sha256
        self.objects[target] = (body, mime_type, sha256)
        return True

    async def overwrite(
        self,
        target: ObjectRef,
        content: BinaryIO,
        *,
        size_bytes: int,
        mime_type: str,
        sha256: str,
    ) -> None:
        body = content.read()
        assert len(body) == size_bytes
        assert hashlib.sha256(body).hexdigest() == sha256
        self.objects[target] = (body, mime_type, sha256)

    async def delete(self, target: ObjectRef) -> None:
        errors = self.delete_errors.get(target)
        if errors:
            raise errors.pop(0)
        self.objects.pop(target, None)

    def read(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        body = self.objects[ref][0]

        async def chunks() -> AsyncIterator[bytes]:
            self.read_count += 1
            errors = self.read_errors.get(ref)
            if errors:
                raise errors.pop(0)
            yield body

        return chunks()

    async def presign_get(self, ref: ObjectRef, expires_in: timedelta) -> str:
        assert ref in self.objects
        assert expires_in == timedelta(minutes=15)
        self.signed.append(ref)
        return f"https://media.example.invalid/{ref.key}"

    async def presign_put(
        self,
        ref: ObjectRef,
        *,
        mime_type: str,
        sha256: str,
        expires_in: timedelta,
    ) -> str:
        self.put_signatures.append((ref, mime_type, sha256, expires_in))
        return f"https://upload.example.invalid/{ref.key}?signature=opaque"


@dataclass
class PodcastHarness:
    admin: AsyncClient
    asset_manager: AsyncClient
    customer: AsyncClient
    customer_without_membership: AsyncClient
    anonymous: AsyncClient
    session_factory: async_sessionmaker[AsyncSession]
    store: FakeObjectStore
    settings: Settings


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
    member_without_membership_id = uuid.uuid4()
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
                User(
                    id=member_without_membership_id,
                    email="unscoped-member@podcast.test",
                    display_name="Unscoped Podcast Listener",
                    password_hash=hash_password("UnscopedPassword123!", pepper),
                    must_change_password=False,
                    system_role=SystemRole.ORG_MEMBER,
                    status=UserStatus.ACTIVE,
                ),
            ]
        )
        await database.flush()
        database.add_all(
            [
                Membership(organization_id=organization_id, user_id=member_id),
                Membership(
                    organization_id=organization_id,
                    user_id=member_without_membership_id,
                ),
            ]
        )

    store = FakeObjectStore()
    app = create_app(settings, ready, session_factory, store)
    clients = [
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") for _ in range(5)
    ]
    try:
        yield PodcastHarness(
            admin=clients[0],
            asset_manager=clients[1],
            customer=clients[2],
            customer_without_membership=clients[3],
            anonymous=clients[4],
            session_factory=session_factory,
            store=store,
            settings=settings,
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
        {"locale": "zh-hant", "title": "市場晨報", "summary": "繁體摘要"},
        {"locale": "zh-hans", "title": "市场晨报", "summary": "简体摘要"},
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
    await _login(
        podcast_harness.customer_without_membership,
        "unscoped-member@podcast.test",
        "UnscopedPassword123!",
    )
    async with podcast_harness.session_factory.begin() as database:
        membership = await database.scalar(
            select(Membership)
            .join(User, User.id == Membership.user_id)
            .where(
                User.email == "unscoped-member@podcast.test",
                Membership.removed_at.is_(None),
            )
        )
        assert membership is not None
        membership.removed_at = datetime.now(UTC)

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
        json={"expected_version": 1},
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
            "locale": "zh-hant",
            "expected_mime_type": "audio/mpeg",
            "reason": "登記手動上傳音檔",
        },
    )
    assert imported.status_code == 200, imported.text
    episode = imported.json()
    assert episode["version"] == 2
    assert episode["audio_variants"][0]["version"] == 1

    published_by_asset_manager = await podcast_harness.asset_manager.post(
        f"/api/admin/podcasts/{episode_id}/publish",
        headers={"X-CSRF-Token": asset_csrf},
        json={"expected_version": 2},
    )
    assert published_by_asset_manager.status_code == 200, published_by_asset_manager.text
    assert published_by_asset_manager.json()["status"] == "published"
    assert published_by_asset_manager.json()["version"] == 3

    unpublished_by_admin = await podcast_harness.admin.post(
        f"/api/admin/podcasts/{episode_id}/unpublish",
        headers={"X-CSRF-Token": admin_csrf},
        json={"expected_version": 3},
    )
    assert unpublished_by_admin.status_code == 200, unpublished_by_admin.text
    assert unpublished_by_admin.json()["status"] == "draft"
    assert unpublished_by_admin.json()["version"] == 4

    published_by_admin = await podcast_harness.admin.post(
        f"/api/admin/podcasts/{episode_id}/publish",
        headers={"X-CSRF-Token": admin_csrf},
        json={"expected_version": 4},
    )
    assert published_by_admin.status_code == 200, published_by_admin.text
    episode = published_by_admin.json()
    assert episode["status"] == "published"
    assert episode["version"] == 5

    catalog = await podcast_harness.customer.get("/api/podcasts?locale=en")
    assert catalog.status_code == 200, catalog.text
    # Text entered through the admin API is what listeners see; the derived
    # "Podcast | date" placeholder only fills the gap when nothing was written.
    assert catalog.json()[0]["title"] == "Market Brief"
    # Fixture bytes are not real audio, so no length could be measured.
    assert catalog.json()[0]["duration_seconds"] is None
    # The release time comes from when the active audio file was registered.
    assert catalog.json()[0]["audio_created_at"] is not None
    detail = await podcast_harness.customer.get(f"/api/podcasts/{episode_id}?locale=zh-hans")
    assert detail.status_code == 200
    assert detail.json()["summary"] == "简体摘要"

    playback = await podcast_harness.customer.post(
        f"/api/podcasts/{episode_id}/audio-url?locale=en"
    )
    assert playback.status_code == 200, playback.text
    assert playback.json()["requested_locale"] == "en"
    assert playback.json()["resolved_locale"] == "zh-hant"
    first_asset_id = playback.json()["asset_id"]

    for internal_customer in (
        podcast_harness.admin,
        podcast_harness.asset_manager,
    ):
        assert (await internal_customer.get("/api/podcasts?locale=en")).status_code == 200
        assert (
            await internal_customer.get(f"/api/podcasts/{episode_id}?locale=en")
        ).status_code == 200
        assert (
            await internal_customer.post(f"/api/podcasts/{episode_id}/audio-url?locale=en")
        ).status_code == 200

    for endpoint, method in (
        ("/api/podcasts", "get"),
        (f"/api/podcasts/{episode_id}", "get"),
        (f"/api/podcasts/{episode_id}/audio-url", "post"),
    ):
        response = await getattr(
            podcast_harness.customer_without_membership,
            method,
        )(endpoint)
        assert response.status_code == 403
        assert response.json()["detail"] == "active organization membership required"

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
            "locale": "zh-hant",
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
            "locale": "zh-hant",
            "expected_mime_type": "audio/mpeg",
            "confirm_replacement": True,
            "expected_current_version": 1,
            "reason": "確認替換音檔",
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["version"] == 6

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
        f"/api/podcasts/{episode_id}/audio-url?locale=zh-hant"
    )
    assert replacement_playback.status_code == 200
    assert replacement_playback.json()["asset_id"] != first_asset_id

    unpublished = await podcast_harness.asset_manager.post(
        f"/api/admin/podcasts/{episode_id}/unpublish",
        headers={"X-CSRF-Token": asset_csrf},
        json={"expected_version": 6},
    )
    assert unpublished.status_code == 200
    assert unpublished.json()["status"] == "draft"
    assert (await podcast_harness.customer.get("/api/podcasts")).json() == []
    unavailable = await podcast_harness.customer.post(f"/api/podcasts/{episode_id}/audio-url")
    assert unavailable.status_code == 404


async def test_browser_upload_uses_fixed_filename_and_any_locale_fallback(
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

    unsupported = await podcast_harness.admin.post(
        "/api/admin/podcasts/uploads",
        headers={"X-CSRF-Token": admin_csrf},
        data={
            "trading_date": "2026-07-25",
            "reason": "initial_upload",
        },
        files={"zh_hans": ("podcast.wav", b"unsupported", "audio/wav")},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["detail"] == {"code": "unsupported_audio_type"}

    uploaded = await podcast_harness.admin.post(
        "/api/admin/podcasts/uploads",
        headers={"X-CSRF-Token": admin_csrf},
        data={
            "trading_date": "2026-07-25",
            "reason": "initial_upload",
        },
        files={
            "zh_hans": (
                "任意來源檔名.mp3",
                b"simplified-chinese-podcast",
                "audio/mpeg",
            )
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    episode = uploaded.json()
    assert episode["status"] == "published"
    assert episode["published_at"] is not None
    assert episode["metadata"][0]["title"] == "Podcast | 2026-07-25"
    assert episode["audio_variants"][0]["locale"] == "zh-hans"
    assert {ref.key for ref in podcast_harness.store.objects} == {
        "podcasts/2026-07-25/audio/zh-hans/podcast.mp3"
    }

    async with podcast_harness.session_factory() as database:
        upload_audit = await database.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "podcast.episode_uploaded",
                AuditEvent.target_id == episode["id"],
            )
        )
        assert upload_audit is not None
        assert upload_audit.before == {"status": "draft"}
        assert upload_audit.after is not None
        assert upload_audit.after["status"] == "published"
        assert upload_audit.after["published_at"] is not None
        assert upload_audit.after["published_by_user_id"] is not None

    catalog = await podcast_harness.customer.get(
        "/api/podcasts",
        params={"locale": "en"},
    )
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()[0]["title"] == "Podcast | 2026-07-25"

    playback = await podcast_harness.customer.post(
        f"/api/podcasts/{episode['id']}/audio-url",
        params={"locale": "en"},
    )
    assert playback.status_code == 200, playback.text
    assert playback.json()["resolved_locale"] == "zh-hans"

    # Fixture bytes carry no chapter tags, so the catalog starts without any;
    # an asset manager can then add markers to the active audio.
    assert catalog.json()[0]["chapters"] == []
    chapters_url = f"/api/admin/podcasts/{episode['id']}/audio/zh-hans/chapters"
    disordered = await podcast_harness.asset_manager.put(
        chapters_url,
        headers={"X-CSRF-Token": asset_csrf},
        json={
            "expected_version": episode["version"],
            "chapters": [
                {"start_seconds": 130, "title": "外資動向"},
                {"start_seconds": 0, "title": "FOMC 決議"},
            ],
            "reason": "chapters",
        },
    )
    assert disordered.status_code == 422, disordered.text
    stale_version = await podcast_harness.asset_manager.put(
        chapters_url,
        headers={"X-CSRF-Token": asset_csrf},
        json={
            "expected_version": episode["version"] + 5,
            "chapters": [{"start_seconds": 0, "title": "FOMC 決議"}],
            "reason": "chapters",
        },
    )
    assert stale_version.status_code == 409, stale_version.text
    missing_locale = await podcast_harness.asset_manager.put(
        f"/api/admin/podcasts/{episode['id']}/audio/en/chapters",
        headers={"X-CSRF-Token": asset_csrf},
        json={
            "expected_version": episode["version"],
            "chapters": [{"start_seconds": 0, "title": "FOMC 決議"}],
            "reason": "chapters",
        },
    )
    assert missing_locale.status_code == 404, missing_locale.text
    chaptered = await podcast_harness.asset_manager.put(
        chapters_url,
        headers={"X-CSRF-Token": asset_csrf},
        json={
            "expected_version": episode["version"],
            "chapters": [
                {"start_seconds": 0, "title": "FOMC 決議"},
                {"start_seconds": 130, "title": "外資動向"},
            ],
            "reason": "chapters",
        },
    )
    assert chaptered.status_code == 200, chaptered.text
    assert chaptered.json()["version"] == episode["version"] + 1
    assert chaptered.json()["audio_variants"][0]["chapters"] == [
        {"start_seconds": 0, "title": "FOMC 決議"},
        {"start_seconds": 130, "title": "外資動向"},
    ]
    episode = chaptered.json()
    chaptered_catalog = await podcast_harness.customer.get(
        "/api/podcasts",
        params={"locale": "en"},
    )
    assert chaptered_catalog.json()[0]["chapters"] == [
        {"start_seconds": 0, "title": "FOMC 決議"},
        {"start_seconds": 130, "title": "外資動向"},
    ]
    async with podcast_harness.session_factory() as database:
        chapters_audit = await database.scalar(
            select(AuditEvent).where(AuditEvent.action == "podcast.audio_chapters_updated")
        )
        assert chapters_audit is not None
        assert chapters_audit.after is not None
        assert chapters_audit.after["locale"] == "zh-hans"

    replacement_warning = await podcast_harness.admin.post(
        "/api/admin/podcasts/uploads",
        headers={"X-CSRF-Token": admin_csrf},
        data={
            "trading_date": "2026-07-25",
            "reason": "update_file",
        },
        files={"zh_hans": ("replacement.mp4", b"replacement", "video/mp4")},
    )
    assert replacement_warning.status_code == 409
    assert replacement_warning.json()["detail"] == {
        "code": "replacement_confirmation_required",
        "current_versions": {"zh-hans": 1},
    }

    replaced = await podcast_harness.admin.post(
        "/api/admin/podcasts/uploads",
        headers={"X-CSRF-Token": admin_csrf},
        data={
            "trading_date": "2026-07-25",
            "reason": "update_file",
            "confirm_replacement": "true",
            "expected_versions": '{"zh-hans": 1}',
        },
        files={"zh_hans": ("replacement.mp4", b"replacement", "video/mp4")},
    )
    assert replaced.status_code == 200, replaced.text
    target = ObjectRef(
        bucket="podcast-private",
        key="podcasts/2026-07-25/audio/zh-hans/podcast.mp4",
    )
    assert set(podcast_harness.store.objects) == {target}
    assert podcast_harness.store.objects[target][0] == b"replacement"
    assert replaced.json()["audio_variants"][0]["version"] == 2
    assert replaced.json()["status"] == "published"
    assert replaced.json()["published_at"] == episode["published_at"]

    existing_draft = await podcast_harness.admin.post(
        "/api/admin/podcasts",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "trading_date": "2026-07-26",
            "metadata": {"values": _metadata()},
            "reason": "建立等待上傳的草稿",
        },
    )
    assert existing_draft.status_code == 201, existing_draft.text
    assert existing_draft.json()["status"] == "draft"

    uploaded_existing_draft = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/uploads",
        headers={"X-CSRF-Token": asset_csrf},
        data={
            "trading_date": "2026-07-26",
            "reason": "initial_upload",
        },
        files={"en": ("existing-draft.mp3", b"existing-draft", "audio/mpeg")},
    )
    assert uploaded_existing_draft.status_code == 200, uploaded_existing_draft.text
    assert uploaded_existing_draft.json()["status"] == "published"
    assert uploaded_existing_draft.json()["published_at"] is not None

    failed_draft = await podcast_harness.admin.post(
        "/api/admin/podcasts",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "trading_date": "2026-07-27",
            "metadata": {"values": _metadata()},
            "reason": "建立失敗上傳測試草稿",
        },
    )
    assert failed_draft.status_code == 201, failed_draft.text

    failed_upload = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/uploads",
        headers={"X-CSRF-Token": asset_csrf},
        data={
            "trading_date": "2026-07-27",
            "reason": "initial_upload",
        },
        files={"en": ("unsupported.wav", b"unsupported", "audio/wav")},
    )
    assert failed_upload.status_code == 422
    episodes_after_failure = (await podcast_harness.admin.get("/api/admin/podcasts")).json()
    unchanged_draft = next(
        item for item in episodes_after_failure if item["id"] == failed_draft.json()["id"]
    )
    assert unchanged_draft["status"] == "draft"
    assert unchanged_draft["published_at"] is None


def _direct_payload(
    *,
    key: str,
    trading_date: str,
    files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    upload_files = files or [
        {
            "locale": "zh-hant",
            "filename": "brief.mp3",
            "size_bytes": 5,
            "mime_type": "audio/mpeg",
        }
    ]
    upload_files = [
        {
            **item,
            "sha256": item.get("sha256", hashlib.sha256(b"nope!").hexdigest()),
        }
        for item in upload_files
    ]
    return {
        "idempotency_key": key,
        "trading_date": trading_date,
        "reason": "initial_upload",
        "files": upload_files,
    }


async def test_direct_upload_authorization_validation_and_idempotency(
    podcast_harness: PodcastHarness,
) -> None:
    unauthenticated = await podcast_harness.anonymous.post(
        "/api/admin/podcasts/upload-batches",
        json=_direct_payload(key="direct-init-anonymous", trading_date="2026-08-01"),
    )
    assert unauthenticated.status_code == 401
    await _login(
        podcast_harness.customer,
        "member@podcast.test",
        "MemberPassword123!",
    )
    forbidden = await podcast_harness.customer.post(
        "/api/admin/podcasts/upload-batches",
        json=_direct_payload(key="direct-init-customer", trading_date="2026-08-01"),
    )
    assert forbidden.status_code == 403

    csrf = await _login(
        podcast_harness.asset_manager,
        "assets@podcast.test",
        "AssetPassword123!",
    )
    payload = _direct_payload(key="direct-init-idempotency", trading_date="2026-08-01")
    expected_sha256 = payload["files"][0]["sha256"]
    missing_csrf = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches", json=payload
    )
    assert missing_csrf.status_code == 403
    initialized = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    )
    assert initialized.status_code == 200, initialized.text
    first = initialized.json()
    file = first["files"][0]
    assert first["base_episode_version"] is None
    assert file["object_key"].endswith(f"/{file['asset_id']}.mp3")
    assert file["required_headers"] == {
        "Content-Type": "audio/mpeg",
        "If-None-Match": "*",
        "x-amz-meta-sha256": expected_sha256,
    }
    assert podcast_harness.store.put_signatures[-1][1:] == (
        "audio/mpeg",
        expected_sha256,
        timedelta(seconds=900),
    )
    replay = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    )
    assert replay.status_code == 200
    replay_file = replay.json()["files"][0]
    assert replay_file["session_id"] == file["session_id"]
    assert replay_file["upload_url"] == file["upload_url"]

    different_payload = {**payload, "reason": "other"}
    mismatch = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=different_payload,
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "idempotency_key_reused"

    wrong_mime = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(
            key="direct-init-bad-mime",
            trading_date="2026-08-02",
            files=[
                {
                    "locale": "zh-hant",
                    "filename": "brief.mp3",
                    "size_bytes": 5,
                    "mime_type": "audio/mp4",
                }
            ],
        ),
    )
    assert wrong_mime.status_code == 422
    oversized = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(
            key="direct-init-too-large",
            trading_date="2026-08-03",
            files=[
                {
                    "locale": "zh-hant",
                    "filename": "brief.mp3",
                    "size_bytes": 256 * 1024 * 1024 + 1,
                    "mime_type": "audio/mpeg",
                }
            ],
        ),
    )
    assert oversized.status_code == 422
    missing_digest_payload = _direct_payload(
        key="direct-init-missing-digest", trading_date="2026-08-11"
    )
    missing_digest_payload["files"][0].pop("sha256")
    missing_digest = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=missing_digest_payload,
    )
    assert missing_digest.status_code == 422
    invalid_digest_payload = _direct_payload(
        key="direct-init-uppercase-digest", trading_date="2026-08-12"
    )
    invalid_digest_payload["files"][0]["sha256"] = "A" * 64
    invalid_digest = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=invalid_digest_payload,
    )
    assert invalid_digest.status_code == 422


async def test_direct_upload_serializes_overlapping_locales_and_recovers_same_key(
    podcast_harness: PodcastHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    same_locale_payload = _direct_payload(
        key="direct-concurrent-same-locale",
        trading_date="2026-08-19",
        files=[
            {
                "locale": "en",
                "filename": "brief.mp3",
                "size_bytes": 5,
                "mime_type": "audio/mpeg",
                "sha256": hashlib.sha256(b"nope!").hexdigest(),
            }
        ],
    )
    overlap_payload = {
        **same_locale_payload,
        "idempotency_key": "direct-concurrent-overlap",
    }
    disjoint_payload = _direct_payload(
        key="direct-concurrent-disjoint",
        trading_date="2026-08-19",
        files=[
            {
                "locale": "zh-hant",
                "filename": "brief.mp3",
                "size_bytes": 5,
                "mime_type": "audio/mpeg",
                "sha256": hashlib.sha256(b"nope!").hexdigest(),
            }
        ],
    )
    presign_entered = asyncio.Event()
    release_presign = asyncio.Event()
    original_presign = podcast_harness.store.presign_put

    async def block_english_presign(
        ref: ObjectRef,
        *,
        mime_type: str,
        sha256: str,
        expires_in: timedelta,
    ) -> str:
        if "/audio/en/" in ref.key and not presign_entered.is_set():
            presign_entered.set()
            await release_presign.wait()
        return await original_presign(
            ref,
            mime_type=mime_type,
            sha256=sha256,
            expires_in=expires_in,
        )

    monkeypatch.setattr(podcast_harness.store, "presign_put", block_english_presign)
    first_task = asyncio.create_task(
        podcast_harness.admin.post(
            "/api/admin/podcasts/upload-batches",
            headers={"X-CSRF-Token": csrf},
            json=same_locale_payload,
        )
    )
    await asyncio.wait_for(presign_entered.wait(), timeout=5)
    replay_task = asyncio.create_task(
        podcast_harness.admin.post(
            "/api/admin/podcasts/upload-batches",
            headers={"X-CSRF-Token": csrf},
            json=same_locale_payload,
        )
    )
    overlap_task = asyncio.create_task(
        podcast_harness.admin.post(
            "/api/admin/podcasts/upload-batches",
            headers={"X-CSRF-Token": csrf},
            json=overlap_payload,
        )
    )
    disjoint_task = asyncio.create_task(
        podcast_harness.admin.post(
            "/api/admin/podcasts/upload-batches",
            headers={"X-CSRF-Token": csrf},
            json=disjoint_payload,
        )
    )
    disjoint_response: Any | None = None
    try:
        await asyncio.sleep(0.05)
        assert not replay_task.done()
        assert not overlap_task.done()
        assert not disjoint_task.done()
    finally:
        release_presign.set()

    first_response, replay_response, overlap_response, disjoint_response = await asyncio.gather(
        first_task,
        replay_task,
        overlap_task,
        disjoint_task,
    )
    assert first_response.status_code == 200, first_response.text
    assert replay_response.status_code == 200, replay_response.text
    assert replay_response.json()["batch_id"] == first_response.json()["batch_id"]
    assert (
        replay_response.json()["files"][0]["upload_url"]
        == first_response.json()["files"][0]["upload_url"]
    )
    assert overlap_response.status_code == 409, overlap_response.text
    assert overlap_response.json()["detail"] == {
        "code": "upload_in_progress",
        "locales": ["en"],
    }
    assert disjoint_response is not None
    assert disjoint_response.status_code == 409, disjoint_response.text
    assert disjoint_response.json()["detail"] == {
        "code": "upload_in_progress",
        "locales": ["en"],
    }

    # A disjoint locale may start after the earlier batch reaches a terminal state.
    async with podcast_harness.session_factory.begin() as database:
        first_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(first_response.json()["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        assert first_session is not None
        first_session.status = "failed"
        first_session.error_code = "test_terminal_failure"
    retried_disjoint = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=disjoint_payload,
    )
    assert retried_disjoint.status_code == 200, retried_disjoint.text


async def test_direct_upload_retry_expires_stale_pending_and_late_finalize_stays_expired(
    podcast_harness: PodcastHarness,
) -> None:
    csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    original_payload = _direct_payload(
        key="direct-init-expired-pending",
        trading_date="2026-08-20",
        files=[
            {
                "locale": "en",
                "filename": "brief.mp3",
                "size_bytes": 5,
                "mime_type": "audio/mpeg",
                "sha256": hashlib.sha256(b"nope!").hexdigest(),
            }
        ],
    )
    original = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=original_payload,
    )
    assert original.status_code == 200, original.text
    original_batch = original.json()
    original_upload = original_batch["files"][0]
    original_ref = ObjectRef(bucket="podcast-private", key=original_upload["object_key"])
    podcast_harness.store.put(
        original_ref,
        b"nope!",
        original_upload["required_headers"]["Content-Type"],
        sha256=original_upload["required_headers"]["x-amz-meta-sha256"],
    )
    async with podcast_harness.session_factory.begin() as database:
        old_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(original_batch["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        assert old_session is not None
        old_session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert old_session.cleanup_after > datetime.now(UTC)

    retry_payload = {
        **original_payload,
        "idempotency_key": "direct-init-expired-pending-retry",
    }
    retry = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=retry_payload,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["batch_id"] != original_batch["batch_id"]
    async with podcast_harness.session_factory() as database:
        old_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(original_batch["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        assert old_session is not None
        assert old_session.status == "expired"
        assert old_session.error_code == "upload_batch_expired"
        assert old_session.cleanup_after > datetime.now(UTC)

    late_finalize = await podcast_harness.admin.post(
        f"/api/admin/podcasts/upload-batches/{original_batch['batch_id']}/files/en/finalize",
        headers={"X-CSRF-Token": csrf},
    )
    assert late_finalize.status_code == 200
    assert late_finalize.json()["status"] == "expired"
    assert late_finalize.json()["error_code"] == "upload_batch_expired"
    assert original_ref in podcast_harness.store.objects
    status = await podcast_harness.admin.get(
        f"/api/admin/podcasts/upload-batches/{original_batch['batch_id']}"
    )
    assert status.status_code == 200
    assert status.json()["status"] == "expired"


@pytest.mark.parametrize("session_status", ["queued", "processing"])
async def test_direct_upload_expired_presign_does_not_expire_queued_work(
    podcast_harness: PodcastHarness,
    session_status: str,
) -> None:
    csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    original = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(key=f"direct-init-old-{session_status}", trading_date="2026-08-21"),
    )
    assert original.status_code == 200, original.text
    batch = original.json()
    upload = batch["files"][0]
    ref = ObjectRef(bucket="podcast-private", key=upload["object_key"])
    podcast_harness.store.put(
        ref,
        b"nope!",
        upload["required_headers"]["Content-Type"],
        sha256=upload["required_headers"]["x-amz-meta-sha256"],
    )
    if session_status == "queued":
        finalized = await podcast_harness.admin.post(
            f"/api/admin/podcasts/upload-batches/{batch['batch_id']}/files/zh-hant/finalize",
            headers={"X-CSRF-Token": csrf},
        )
        assert finalized.status_code == 200
        assert finalized.json()["status"] == "queued"
    async with podcast_harness.session_factory.begin() as database:
        session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "zh-hant",
            )
        )
        assert session is not None
        session.status = session_status
        session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        if session_status == "processing":
            session.lease_token = uuid.uuid4()
            session.lease_until = datetime.now(UTC) + timedelta(minutes=1)

    retry = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(
            key=f"direct-init-old-{session_status}-retry",
            trading_date="2026-08-21",
        ),
    )
    assert retry.status_code == 409
    assert retry.json()["detail"] == {
        "code": "upload_in_progress",
        "locales": ["zh-hant"],
    }


async def test_direct_upload_replacement_requires_current_locale_confirmation(
    podcast_harness: PodcastHarness,
) -> None:
    csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    async with podcast_harness.session_factory.begin() as database:
        user = await database.scalar(select(User).where(User.email == "admin@podcast.test"))
        assert user is not None
        episode = PodcastEpisode(
            trading_date=date.fromisoformat("2026-08-04"),
            status="draft",
            version=2,
            created_by_user_id=user.id,
        )
        asset = Asset(
            bucket="podcast-private",
            object_key="existing/zh-hant.mp3",
            kind=AssetKind.AUDIO,
            mime_type="audio/mpeg",
            size_bytes=3,
            sha256=hashlib.sha256(b"old").hexdigest(),
            locale="zh-hant",
            localized_titles={},
            uploaded_by_user_id=user.id,
        )
        database.add_all([episode, asset])
        await database.flush()
        database.add(
            PodcastEpisodeAudioVariant(
                episode_id=episode.id,
                locale="zh-hant",
                version=1,
                asset_id=asset.id,
                is_active=True,
                activated_by_user_id=user.id,
            )
        )

    url = "/api/admin/podcasts/upload-batches"
    base = _direct_payload(key="direct-replace-check", trading_date="2026-08-04")
    warning = await podcast_harness.admin.post(url, headers={"X-CSRF-Token": csrf}, json=base)
    assert warning.status_code == 409
    assert warning.json()["detail"] == {
        "code": "replacement_confirmation_required",
        "current_versions": {"zh-hant": 1},
    }
    stale = await podcast_harness.admin.post(
        url,
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(
            key="direct-replace-check",
            trading_date="2026-08-04",
            files=[
                {
                    "locale": "zh-hant",
                    "filename": "brief.mp3",
                    "size_bytes": 5,
                    "mime_type": "audio/mpeg",
                    "confirm_replacement": True,
                    "expected_current_version": 2,
                }
            ],
        ),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "audio_version_conflict",
        "current_versions": {"zh-hant": 1},
    }
    accepted = await podcast_harness.admin.post(
        url,
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(
            key="direct-replace-check",
            trading_date="2026-08-04",
            files=[
                {
                    "locale": "zh-hant",
                    "filename": "brief.mp3",
                    "size_bytes": 5,
                    "mime_type": "audio/mpeg",
                    "confirm_replacement": True,
                    "expected_current_version": 1,
                }
            ],
        ),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["base_episode_version"] == 2


async def test_direct_upload_finalize_mismatch_and_missing_object_retry(
    podcast_harness: PodcastHarness,
) -> None:
    csrf = await _login(
        podcast_harness.asset_manager,
        "assets@podcast.test",
        "AssetPassword123!",
    )
    initialized = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(key="direct-finalize-retry", trading_date="2026-08-05"),
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    upload = batch["files"][0]
    finalize_path = (
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}/files/{upload['locale']}/finalize"
    )
    missing = await podcast_harness.asset_manager.post(
        finalize_path, headers={"X-CSRF-Token": csrf}
    )
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "object_not_uploaded"
    ref = ObjectRef(bucket="podcast-private", key=upload["object_key"])
    podcast_harness.store.put(
        ref,
        b"nope!",
        "audio/mpeg",
        sha256=upload["required_headers"]["x-amz-meta-sha256"],
    )
    queued = await podcast_harness.asset_manager.post(finalize_path, headers={"X-CSRF-Token": csrf})
    assert queued.status_code == 200
    assert queued.json()["status"] == "queued"
    retry = await podcast_harness.asset_manager.post(finalize_path, headers={"X-CSRF-Token": csrf})
    assert retry.status_code == 200
    assert retry.json()["status"] == "queued"

    wrong_size_init = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(
            key="direct-finalize-bad-size",
            trading_date="2026-08-06",
            files=[
                {
                    "locale": "en",
                    "filename": "brief.mp3",
                    "size_bytes": 4,
                    "mime_type": "audio/mpeg",
                }
            ],
        ),
    )
    bad = wrong_size_init.json()
    bad_file = bad["files"][0]
    bad_ref = ObjectRef(bucket="podcast-private", key=bad_file["object_key"])
    podcast_harness.store.put(bad_ref, b"bad", "audio/mpeg")
    bad_finalize = await podcast_harness.asset_manager.post(
        f"/api/admin/podcasts/upload-batches/{bad['batch_id']}/files/en/finalize",
        headers={"X-CSRF-Token": csrf},
    )
    assert bad_finalize.status_code == 200
    assert bad_finalize.json()["status"] == "failed"
    assert bad_finalize.json()["error_code"] == "uploaded_size_mismatch"


@pytest.mark.parametrize(
    ("object_sha256", "expected_error"),
    [(None, "uploaded_sha256_missing"), ("0" * 64, "uploaded_sha256_mismatch")],
)
async def test_finalize_requires_expected_r2_checksum_metadata(
    podcast_harness: PodcastHarness,
    object_sha256: str | None,
    expected_error: str,
) -> None:
    csrf = await _login(
        podcast_harness.asset_manager,
        "assets@podcast.test",
        "AssetPassword123!",
    )
    initialized = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(key=f"finalize-checksum-{expected_error}", trading_date="2026-08-13"),
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    upload = batch["files"][0]
    podcast_harness.store.put(
        ObjectRef(bucket="podcast-private", key=upload["object_key"]),
        b"nope!",
        "audio/mpeg",
        sha256=object_sha256,
    )

    finalized = await podcast_harness.asset_manager.post(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}/files/{upload['locale']}/finalize",
        headers={"X-CSRF-Token": csrf},
    )

    assert finalized.status_code == 200
    assert finalized.json()["status"] == "failed"
    assert finalized.json()["error_code"] == expected_error


@pytest.mark.parametrize(
    ("metadata_sha256", "expected_error"),
    [(None, "object_sha256_missing"), ("0" * 64, "object_sha256_mismatch")],
)
async def test_worker_rechecks_r2_checksum_metadata_after_finalize(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
    metadata_sha256: str | None,
    expected_error: str,
) -> None:
    csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    initialized = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(key=f"worker-checksum-{expected_error}", trading_date="2026-08-14"),
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    upload = batch["files"][0]
    ref = ObjectRef(bucket="podcast-private", key=upload["object_key"])
    expected_sha256 = upload["required_headers"]["x-amz-meta-sha256"]
    podcast_harness.store.put(ref, b"nope!", "audio/mpeg", sha256=expected_sha256)
    finalized = await podcast_harness.admin.post(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}/files/{upload['locale']}/finalize",
        headers={"X-CSRF-Token": csrf},
    )
    assert finalized.json()["status"] == "queued"
    podcast_harness.store.put(ref, b"nope!", "audio/mpeg", sha256=metadata_sha256)
    worker = PodcastMediaWorker(
        podcast_harness.session_factory,
        podcast_harness.store,
        podcast_harness.settings,
        heartbeat_path=tmp_path / "media-worker-heartbeat",
        spool_directory=tmp_path,
    )

    assert await worker.process_one()
    result = await podcast_harness.admin.get(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}"
    )
    assert result.json()["files"][0]["status"] == "failed"
    assert result.json()["files"][0]["error_code"] == expected_error
    async with podcast_harness.session_factory() as database:
        assert (
            await database.scalar(select(Asset).where(Asset.object_key == upload["object_key"]))
            is None
        )


async def test_media_worker_verifies_sha_publishes_per_locale_and_enables_playback(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
) -> None:
    csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    await _login(podcast_harness.customer, "member@podcast.test", "MemberPassword123!")
    initialized = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(
            key="direct-worker-cutover",
            trading_date="2026-08-07",
            files=[
                {
                    "locale": "zh-hant",
                    "filename": "brief.mp3",
                    "size_bytes": 5,
                    "mime_type": "audio/mpeg",
                    "sha256": hashlib.sha256(b"hant!").hexdigest(),
                },
                {
                    "locale": "en",
                    "filename": "brief.mp4",
                    "size_bytes": 5,
                    "mime_type": "audio/mp4",
                    "sha256": hashlib.sha256(b"eng!!").hexdigest(),
                },
            ],
        ),
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    bodies = {"zh-hant": b"hant!", "en": b"eng!!"}
    for file in batch["files"]:
        podcast_harness.store.put(
            ObjectRef(bucket="podcast-private", key=file["object_key"]),
            bodies[file["locale"]],
            file["required_headers"]["Content-Type"],
            sha256=file["required_headers"]["x-amz-meta-sha256"],
        )
        finalized = await podcast_harness.admin.post(
            f"/api/admin/podcasts/upload-batches/{batch['batch_id']}"
            f"/files/{file['locale']}/finalize",
            headers={"X-CSRF-Token": csrf},
        )
        assert finalized.status_code == 200
        assert finalized.json()["status"] == "queued"

    worker = PodcastMediaWorker(
        podcast_harness.session_factory,
        podcast_harness.store,
        podcast_harness.settings,
        heartbeat_path=tmp_path / "media-worker-heartbeat",
        spool_directory=tmp_path,
    )
    assert await worker.process_one()
    async with podcast_harness.session_factory() as database:
        episode = await database.scalar(
            select(PodcastEpisode).where(
                PodcastEpisode.trading_date == date.fromisoformat("2026-08-07")
            )
        )
        assert episode is not None
        assert episode.version == 2
        assert episode.status == "published"
        episode_id = episode.id
    assert await worker.process_one()

    status_response = await podcast_harness.admin.get(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}"
    )
    assert status_response.status_code == 200, status_response.text
    assert status_response.json()["status"] == "completed"
    assert {item["status"] for item in status_response.json()["files"]} == {"completed"}
    for item in status_response.json()["files"]:
        assert item["sha256"] == hashlib.sha256(bodies[item["locale"]]).hexdigest()
        assert item["duration_seconds"] is None

    async with podcast_harness.session_factory() as database:
        episode = await database.scalar(
            select(PodcastEpisode).where(PodcastEpisode.id == episode_id)
        )
        assert episode is not None and episode.version == 3
        active = (
            await database.scalars(
                select(PodcastEpisodeAudioVariant).where(
                    PodcastEpisodeAudioVariant.episode_id == episode_id,
                    PodcastEpisodeAudioVariant.is_active.is_(True),
                )
            )
        ).all()
        assert {row.locale: row.version for row in active} == {"zh-hant": 1, "en": 1}
        assets = (
            await database.scalars(
                select(Asset).where(Asset.id.in_([row.asset_id for row in active]))
            )
        ).all()
        assert {asset.sha256 for asset in assets} == {
            hashlib.sha256(body).hexdigest() for body in bodies.values()
        }

    catalog = await podcast_harness.customer.get("/api/podcasts?locale=en")
    assert catalog.status_code == 200
    assert catalog.json()[0]["id"] == str(episode_id)
    reads_before_playback_signing = podcast_harness.store.read_count
    playback = await podcast_harness.customer.post(
        f"/api/podcasts/{episode_id}/audio-url?locale=en"
    )
    assert playback.status_code == 200, playback.text
    assert playback.json()["resolved_locale"] == "en"
    assert podcast_harness.store.read_count == reads_before_playback_signing


async def test_media_worker_batch_fence_prevents_republish_after_unpublish(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
) -> None:
    csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    initialized = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(
            key="direct-worker-unpublish-fence",
            trading_date="2026-08-08",
            files=[
                {
                    "locale": locale,
                    "filename": "brief.mp3",
                    "size_bytes": 5,
                    "mime_type": "audio/mpeg",
                    "sha256": hashlib.sha256(b"bytes").hexdigest(),
                }
                for locale in ("zh-hant", "en")
            ],
        ),
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    for file in batch["files"]:
        ref = ObjectRef(bucket="podcast-private", key=file["object_key"])
        podcast_harness.store.put(
            ref,
            b"bytes",
            "audio/mpeg",
            sha256=file["required_headers"]["x-amz-meta-sha256"],
        )
        finalized = await podcast_harness.admin.post(
            f"/api/admin/podcasts/upload-batches/{batch['batch_id']}"
            f"/files/{file['locale']}/finalize",
            headers={"X-CSRF-Token": csrf},
        )
        assert finalized.json()["status"] == "queued"

    worker = PodcastMediaWorker(
        podcast_harness.session_factory,
        podcast_harness.store,
        podcast_harness.settings,
        heartbeat_path=tmp_path / "media-worker-heartbeat",
        spool_directory=tmp_path,
    )
    assert await worker.process_one()
    async with podcast_harness.session_factory() as database:
        episode = await database.scalar(
            select(PodcastEpisode).where(
                PodcastEpisode.trading_date == date.fromisoformat("2026-08-08")
            )
        )
        assert episode is not None and episode.version == 2
        episode_id = episode.id
    unpublished = await podcast_harness.admin.post(
        f"/api/admin/podcasts/{episode_id}/unpublish",
        headers={"X-CSRF-Token": csrf},
        json={"expected_version": 2},
    )
    assert unpublished.status_code == 200
    assert unpublished.json()["status"] == "draft"
    assert unpublished.json()["version"] == 3

    assert await worker.process_one()
    result = await podcast_harness.admin.get(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}"
    )
    assert result.status_code == 200
    assert result.json()["status"] == "conflict"
    assert {item["status"] for item in result.json()["files"]} == {"completed", "conflict"}
    assert (
        next(item for item in result.json()["files"] if item["status"] == "conflict")["error_code"]
        == "episode_version_conflict"
    )
    conflict_locale = next(
        item["locale"] for item in result.json()["files"] if item["status"] == "conflict"
    )
    conflict_upload = next(file for file in batch["files"] if file["locale"] == conflict_locale)
    conflict_ref = ObjectRef(bucket="podcast-private", key=conflict_upload["object_key"])
    async with podcast_harness.session_factory.begin() as database:
        conflict_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == conflict_locale,
            )
        )
        assert conflict_session is not None and conflict_session.status == "conflict"
        conflict_session.cleanup_after = datetime.now(UTC) - timedelta(seconds=1)

    assert await worker.cleanup_one()
    assert conflict_ref not in podcast_harness.store.objects


async def test_media_worker_hash_mismatch_never_activates_audio(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
) -> None:
    csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    initialized = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(key="direct-worker-hash-mismatch", trading_date="2026-08-10"),
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    upload = batch["files"][0]
    ref = ObjectRef(bucket="podcast-private", key=upload["object_key"])
    podcast_harness.store.put(
        ref,
        b"bytes",
        "audio/mpeg",
        sha256=upload["required_headers"]["x-amz-meta-sha256"],
    )
    finalized = await podcast_harness.admin.post(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}/files/{upload['locale']}/finalize",
        headers={"X-CSRF-Token": csrf},
    )
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "queued"
    worker = PodcastMediaWorker(
        podcast_harness.session_factory,
        podcast_harness.store,
        podcast_harness.settings,
        heartbeat_path=tmp_path / "media-worker-heartbeat",
        spool_directory=tmp_path,
    )
    assert await worker.process_one()
    result = await podcast_harness.admin.get(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}"
    )
    assert result.json()["files"][0]["status"] == "failed"
    assert result.json()["files"][0]["error_code"] == "object_sha256_mismatch"
    async with podcast_harness.session_factory() as database:
        assert (
            await database.scalar(
                select(PodcastEpisode).where(
                    PodcastEpisode.trading_date == date.fromisoformat("2026-08-10")
                )
            )
            is None
        )
        assert (
            await database.scalar(select(Asset).where(Asset.object_key == upload["object_key"]))
            is None
        )


@pytest.mark.parametrize("failure_stage", ["head", "read"])
async def test_media_worker_retries_store_failures_without_blocking_queued_locale(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
    failure_stage: str,
) -> None:
    csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    body = b"retry"
    checksum = hashlib.sha256(body).hexdigest()
    initialized = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(
            key=f"direct-worker-{failure_stage}-retry",
            trading_date="2026-08-15",
            files=[
                {
                    "locale": locale,
                    "filename": "brief.mp3",
                    "size_bytes": len(body),
                    "mime_type": "audio/mpeg",
                    "sha256": checksum,
                }
                for locale in ("en", "zh-hant")
            ],
        ),
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    uploads = {file["locale"]: file for file in batch["files"]}
    for file in uploads.values():
        ref = ObjectRef(bucket="podcast-private", key=file["object_key"])
        podcast_harness.store.put(
            ref,
            body,
            file["required_headers"]["Content-Type"],
            sha256=file["required_headers"]["x-amz-meta-sha256"],
        )
        finalized = await podcast_harness.admin.post(
            f"/api/admin/podcasts/upload-batches/{batch['batch_id']}"
            f"/files/{file['locale']}/finalize",
            headers={"X-CSRF-Token": csrf},
        )
        assert finalized.status_code == 200
        assert finalized.json()["status"] == "queued"

    failing_ref = ObjectRef(bucket="podcast-private", key=uploads["en"]["object_key"])
    if failure_stage == "head":
        podcast_harness.store.head_errors[failing_ref] = [
            ClientError(
                {
                    "Error": {"Code": "AccessDenied", "Message": "permission denied"},
                    "ResponseMetadata": {
                        "HTTPStatusCode": 403,
                        "HTTPHeaders": {},
                        "HostId": "",
                        "RequestId": "",
                        "RetryAttempts": 0,
                    },
                },
                "HeadObject",
            )
        ]
    else:
        podcast_harness.store.read_errors[failing_ref] = [
            EndpointConnectionError(endpoint_url="https://r2.example.invalid")
        ]

    # Make selection deterministic: the injected failure belongs to the first claim.
    async with podcast_harness.session_factory.begin() as database:
        en_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        zh_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "zh-hant",
            )
        )
        assert en_session is not None and zh_session is not None
        en_session.created_at = datetime.now(UTC) - timedelta(minutes=1)
        zh_session.created_at = datetime.now(UTC)

    worker = PodcastMediaWorker(
        podcast_harness.session_factory,
        podcast_harness.store,
        podcast_harness.settings,
        heartbeat_path=tmp_path / f"media-worker-{failure_stage}-retry-heartbeat",
        spool_directory=tmp_path,
    )
    assert await worker.process_one()
    async with podcast_harness.session_factory.begin() as database:
        en_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        assert en_session is not None
        assert en_session.status == "processing"
        assert en_session.error_code == "object_store_retry_scheduled"
        assert en_session.lease_token is None
        assert en_session.lease_until is not None
        assert en_session.lease_until > datetime.now(UTC)
        assert en_session.attempts == 1
        assert (en_session.lease_until - datetime.now(UTC)).total_seconds() <= RETRY_BASE_SECONDS

    # The delayed retry is not claimable, so the next queued locale can proceed.
    assert await worker.process_one()
    async with podcast_harness.session_factory.begin() as database:
        zh_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "zh-hant",
            )
        )
        assert zh_session is not None and zh_session.status == "completed"
        en_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        assert en_session is not None
        en_session.lease_until = datetime.now(UTC) - timedelta(seconds=1)

    assert await worker.process_one()
    async with podcast_harness.session_factory.begin() as database:
        en_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        assert en_session is not None and en_session.status == "completed"
        assert en_session.attempts == 2


async def test_media_worker_object_store_retry_exhaustion_is_terminal(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
) -> None:
    csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    initialized = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(key="direct-worker-retry-exhaustion", trading_date="2026-08-16"),
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    upload = batch["files"][0]
    ref = ObjectRef(bucket="podcast-private", key=upload["object_key"])
    body = b"bytes"
    podcast_harness.store.put(
        ref,
        body,
        upload["required_headers"]["Content-Type"],
        sha256=upload["required_headers"]["x-amz-meta-sha256"],
    )
    finalized = await podcast_harness.admin.post(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}/files/{upload['locale']}/finalize",
        headers={"X-CSRF-Token": csrf},
    )
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "queued"
    podcast_harness.store.head_errors[ref] = [
        EndpointConnectionError(endpoint_url="https://r2.example.invalid")
        for _ in range(MAX_PROCESSING_ATTEMPTS)
    ]
    worker = PodcastMediaWorker(
        podcast_harness.session_factory,
        podcast_harness.store,
        podcast_harness.settings,
        heartbeat_path=tmp_path / "media-worker-retry-exhaustion-heartbeat",
        spool_directory=tmp_path,
    )

    for attempt in range(1, MAX_PROCESSING_ATTEMPTS + 1):
        assert await worker.process_one()
        async with podcast_harness.session_factory.begin() as database:
            session = await database.scalar(
                select(PodcastUploadSession).where(
                    PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                    PodcastUploadSession.locale == upload["locale"],
                )
            )
            assert session is not None
            assert session.attempts == attempt
            if attempt < MAX_PROCESSING_ATTEMPTS:
                assert session.status == "processing"
                assert session.error_code == "object_store_retry_scheduled"
                assert session.lease_until is not None
                session.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            else:
                assert session.status == "failed"
                assert session.error_code == "object_store_retry_exhausted"
                assert session.lease_until is None
                assert session.lease_token is None

    assert not await worker.process_one()


async def test_media_worker_does_not_swallow_unexpected_store_adapter_errors(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
) -> None:
    csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    initialized = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(key="direct-worker-unexpected-error", trading_date="2026-08-17"),
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    upload = batch["files"][0]
    ref = ObjectRef(bucket="podcast-private", key=upload["object_key"])
    body = b"bytes"
    podcast_harness.store.put(
        ref,
        body,
        upload["required_headers"]["Content-Type"],
        sha256=upload["required_headers"]["x-amz-meta-sha256"],
    )
    finalized = await podcast_harness.admin.post(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}/files/{upload['locale']}/finalize",
        headers={"X-CSRF-Token": csrf},
    )
    assert finalized.status_code == 200
    podcast_harness.store.head_errors[ref] = [RuntimeError("adapter programming defect")]
    worker = PodcastMediaWorker(
        podcast_harness.session_factory,
        podcast_harness.store,
        podcast_harness.settings,
        heartbeat_path=tmp_path / "media-worker-unexpected-error-heartbeat",
        spool_directory=tmp_path,
    )

    with pytest.raises(RuntimeError, match="adapter programming defect"):
        await worker.process_one()
    async with podcast_harness.session_factory.begin() as database:
        session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == upload["locale"],
            )
        )
        assert session is not None
        assert session.status == "processing"
        assert session.error_code is None
        assert session.lease_token is not None


async def test_expired_upload_cleanup_resweeps_late_objects_and_respects_assets(
    podcast_harness: PodcastHarness,
) -> None:
    csrf = await _login(
        podcast_harness.asset_manager,
        "assets@podcast.test",
        "AssetPassword123!",
    )
    initialized = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(
            key="direct-cleanup-tombstones",
            trading_date="2026-08-09",
            files=[
                {
                    "locale": "zh-hant",
                    "filename": "hant.mp3",
                    "size_bytes": 3,
                    "mime_type": "audio/mpeg",
                },
                {
                    "locale": "en",
                    "filename": "english.mp3",
                    "size_bytes": 3,
                    "mime_type": "audio/mpeg",
                },
            ],
        ),
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    by_locale = {file["locale"]: file for file in batch["files"]}
    untracked_ref = ObjectRef(bucket="podcast-private", key=by_locale["zh-hant"]["object_key"])
    protected_ref = ObjectRef(bucket="podcast-private", key=by_locale["en"]["object_key"])
    podcast_harness.store.put(untracked_ref, b"old", "audio/mpeg")
    podcast_harness.store.put(protected_ref, b"old", "audio/mpeg")
    async with podcast_harness.session_factory.begin() as database:
        user = await database.scalar(select(User).where(User.email == "assets@podcast.test"))
        assert user is not None
        protected = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        untracked = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "zh-hant",
            )
        )
        assert protected is not None and untracked is not None
        future = datetime.now(UTC) + timedelta(hours=1)
        protected.cleanup_after = future
        untracked.cleanup_after = future
        database.add(
            Asset(
                id=protected.asset_id,
                bucket=protected_ref.bucket,
                object_key=protected_ref.key,
                kind=AssetKind.AUDIO,
                mime_type="audio/mpeg",
                size_bytes=3,
                sha256=hashlib.sha256(b"old").hexdigest(),
                locale="en",
                localized_titles={},
                uploaded_by_user_id=user.id,
            )
        )
    worker = PodcastMediaWorker(
        podcast_harness.session_factory,
        podcast_harness.store,
        podcast_harness.settings,
    )
    assert not await worker.cleanup_one()
    async with podcast_harness.session_factory.begin() as database:
        sessions = (
            await database.scalars(
                select(PodcastUploadSession).where(
                    PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"])
                )
            )
        ).all()
        for session in sessions:
            if session.locale == "zh-hant":
                session.cleanup_after = datetime.now(UTC) - timedelta(seconds=1)
    assert await worker.cleanup_one()
    assert untracked_ref not in podcast_harness.store.objects
    async with podcast_harness.session_factory.begin() as database:
        untracked = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "zh-hant",
            )
        )
        assert untracked is not None and untracked.status == "expired"
        untracked.cleanup_lease_until = datetime.now(UTC) - timedelta(seconds=1)
    podcast_harness.store.put(untracked_ref, b"late", "audio/mpeg")
    assert await worker.cleanup_one()
    assert untracked_ref not in podcast_harness.store.objects

    # The object key is considered protected once an Asset row owns it.
    async with podcast_harness.session_factory.begin() as database:
        protected = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        assert protected is not None
        protected.cleanup_after = datetime.now(UTC) - timedelta(seconds=1)
        protected.cleanup_lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert await worker.cleanup_one()
    assert protected_ref in podcast_harness.store.objects


@pytest.mark.parametrize("failure_operation", ["head", "delete"])
async def test_cleanup_store_errors_defer_and_retry_without_blocking_queued_uploads(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
    failure_operation: str,
) -> None:
    csrf = await _login(
        podcast_harness.asset_manager,
        "assets@podcast.test",
        "AssetPassword123!",
    )
    body = b"queue"
    checksum = hashlib.sha256(body).hexdigest()
    initialized = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/upload-batches",
        headers={"X-CSRF-Token": csrf},
        json=_direct_payload(
            key=f"direct-cleanup-{failure_operation}-retry",
            trading_date="2026-08-18",
            files=[
                {
                    "locale": locale,
                    "filename": "brief.mp3",
                    "size_bytes": len(body),
                    "mime_type": "audio/mpeg",
                    "sha256": checksum,
                }
                for locale in ("en", "zh-hant")
            ],
        ),
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    uploads = {file["locale"]: file for file in batch["files"]}
    cleanup_upload = uploads["en"]
    cleanup_ref = ObjectRef(bucket="podcast-private", key=cleanup_upload["object_key"])
    podcast_harness.store.put(
        cleanup_ref,
        body,
        cleanup_upload["required_headers"]["Content-Type"],
        sha256=cleanup_upload["required_headers"]["x-amz-meta-sha256"],
    )

    queued_upload = uploads["zh-hant"]
    queued_ref = ObjectRef(bucket="podcast-private", key=queued_upload["object_key"])
    podcast_harness.store.put(
        queued_ref,
        body,
        queued_upload["required_headers"]["Content-Type"],
        sha256=queued_upload["required_headers"]["x-amz-meta-sha256"],
    )
    finalized = await podcast_harness.asset_manager.post(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}"
        f"/files/{queued_upload['locale']}/finalize",
        headers={"X-CSRF-Token": csrf},
    )
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "queued"
    async with podcast_harness.session_factory.begin() as database:
        cleanup_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        assert cleanup_session is not None
        cleanup_session.cleanup_after = datetime.now(UTC) - timedelta(seconds=1)

    if failure_operation == "head":
        podcast_harness.store.head_errors[cleanup_ref] = [
            EndpointConnectionError(endpoint_url="https://r2.example.invalid")
        ]
    else:
        podcast_harness.store.delete_errors[cleanup_ref] = [
            ClientError(
                {
                    "Error": {"Code": "ServiceUnavailable", "Message": "temporary failure"},
                    "ResponseMetadata": {
                        "HTTPStatusCode": 503,
                        "HTTPHeaders": {},
                        "HostId": "",
                        "RequestId": "",
                        "RetryAttempts": 0,
                    },
                },
                "DeleteObject",
            )
        ]

    worker = PodcastMediaWorker(
        podcast_harness.session_factory,
        podcast_harness.store,
        podcast_harness.settings,
        heartbeat_path=tmp_path / f"media-worker-cleanup-{failure_operation}-heartbeat",
        spool_directory=tmp_path,
    )
    assert await worker.cleanup_one()
    async with podcast_harness.session_factory.begin() as database:
        cleanup_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        assert cleanup_session is not None
        assert cleanup_session.status == "expired"
        assert cleanup_session.cleanup_lease_token is None
        assert cleanup_session.cleanup_lease_until is not None
        assert cleanup_session.cleanup_lease_until > datetime.now(UTC)
    assert cleanup_ref in podcast_harness.store.objects
    assert not await worker.cleanup_one()

    # Cleanup backoff does not block normal media work in the same worker.
    assert await worker.process_one()
    async with podcast_harness.session_factory.begin() as database:
        queued_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "zh-hant",
            )
        )
        assert queued_session is not None and queued_session.status == "completed"
        cleanup_session = await database.scalar(
            select(PodcastUploadSession).where(
                PodcastUploadSession.batch_id == uuid.UUID(batch["batch_id"]),
                PodcastUploadSession.locale == "en",
            )
        )
        assert cleanup_session is not None
        cleanup_session.cleanup_lease_until = datetime.now(UTC) - timedelta(seconds=1)

    assert await worker.cleanup_one()
    assert cleanup_ref not in podcast_harness.store.objects
