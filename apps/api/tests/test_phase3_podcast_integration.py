import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

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
from daily_insights_api.modules.audit.models import AuditEvent
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.podcasts.analysis import (
    PodcastAnalysisError,
    PodcastAnalyzer,
    Transcript,
    TranscriptSegment,
)
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
        self.objects.pop(target, None)

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


class FakeTranscriber:
    """Returns a fixed four-block transcript regardless of the audio bytes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def transcribe(
        self, content: bytes, *, filename: str, mime_type: str, language: str | None
    ) -> Transcript:
        self.calls.append((filename, mime_type, language))
        return Transcript(
            language="chinese",
            duration=100.0,
            text="開場 外資 匯率 清單",
            segments=tuple(
                TranscriptSegment(start=index * 25, end=index * 25 + 25, text=text)
                for index, text in enumerate(["開場", "外資", "匯率", "清單"])
            ),
        )


class FakeAnalysisModel:
    """Fails unless a test opts in, so uploads in unrelated tests keep their
    derived titles while the background analysis records a failure."""

    def __init__(self) -> None:
        self.prompts: list[dict[str, object]] = []
        self.succeed = False

    async def complete_json(self, prompt: dict[str, object]) -> dict[str, object]:
        self.prompts.append(prompt)
        if not self.succeed:
            raise PodcastAnalysisError("analysis disabled in this test")
        return {
            "title": {"zh-hant": "AI 標題", "zh-hans": "AI 标题", "en": "AI title"},
            "summary": {"zh-hant": "AI 摘要", "zh-hans": "AI 摘要", "en": "AI summary"},
            "chapters": [
                {"block": 0, "title": {"zh-hant": "開場", "zh-hans": "开场", "en": "Open"}},
                {"block": 2, "title": {"zh-hant": "匯率", "zh-hans": "汇率", "en": "FX"}},
                {"block": 3, "title": {"zh-hant": "清單", "zh-hans": "清单", "en": "List"}},
            ],
        }


@dataclass
class PodcastHarness:
    admin: AsyncClient
    asset_manager: AsyncClient
    customer: AsyncClient
    customer_without_membership: AsyncClient
    anonymous: AsyncClient
    session_factory: async_sessionmaker[AsyncSession]
    store: FakeObjectStore
    analyzer: PodcastAnalyzer
    transcriber: FakeTranscriber
    model: FakeAnalysisModel


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
    transcriber = FakeTranscriber()
    model = FakeAnalysisModel()
    analyzer = PodcastAnalyzer(
        session_factory=session_factory,
        store=store,
        transcriber=transcriber,
        model=model,
    )
    app = create_app(settings, ready, session_factory, store, podcast_analyzer=analyzer)
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
            analyzer=analyzer,
            transcriber=transcriber,
            model=model,
        )
    finally:
        await analyzer.wait_for_scheduled()
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
    # an asset manager can then add markers to the active audio. The
    # background analysis fails in this harness and leaves everything as is.
    await podcast_harness.analyzer.wait_for_scheduled()
    assert catalog.json()[0]["chapters"] == []
    failed_variant = (await podcast_harness.admin.get("/api/admin/podcasts")).json()[0][
        "audio_variants"
    ][0]
    assert failed_variant["analysis_status"] == "failed"
    assert failed_variant["analysis_error"] == "analysis disabled in this test"
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


async def test_upload_triggers_analysis_that_titles_and_chapters_the_episode(
    podcast_harness: PodcastHarness,
) -> None:
    admin_csrf = await _login(
        podcast_harness.admin,
        "admin@podcast.test",
        "AdminPassword123!",
    )
    await _login(
        podcast_harness.customer,
        "member@podcast.test",
        "MemberPassword123!",
    )
    podcast_harness.model.succeed = True

    uploaded = await podcast_harness.admin.post(
        "/api/admin/podcasts/uploads",
        headers={"X-CSRF-Token": admin_csrf},
        data={"trading_date": "2026-09-05", "reason": "initial_upload"},
        files={"zh_hant": ("podcast.mp3", b"traditional-chinese-podcast", "audio/mpeg")},
    )
    assert uploaded.status_code == 200, uploaded.text
    episode_id = uploaded.json()["id"]
    assert uploaded.json()["metadata_source"] == "derived"
    assert uploaded.json()["audio_variants"][0]["analysis_status"] == "none"

    # The upload response returns before the background analysis finishes.
    await podcast_harness.analyzer.wait_for_scheduled()
    assert podcast_harness.transcriber.calls == [("podcast.mp3", "audio/mpeg", "zh")]

    listed = await podcast_harness.admin.get("/api/admin/podcasts")
    episode = next(item for item in listed.json() if item["id"] == episode_id)
    assert episode["metadata_source"] == "ai"
    # Analysis is not an editorial action, so the version the admin loaded
    # right after uploading still works for their next save.
    assert episode["version"] == uploaded.json()["version"]
    assert episode["metadata"][0] == {
        "locale": "zh-hant",
        "title": "AI 標題",
        "summary": "AI 摘要",
    }
    variant = episode["audio_variants"][0]
    assert variant["analysis_status"] == "succeeded"
    assert variant["analyzed_at"] is not None
    assert variant["chapters_source"] == "ai"
    assert variant["chapters"] == [
        {"start_seconds": 0, "title": "開場"},
        {"start_seconds": 50, "title": "匯率"},
        {"start_seconds": 75, "title": "清單"},
    ]
    catalog = await podcast_harness.customer.get("/api/podcasts", params={"locale": "en"})
    assert catalog.json()[0]["title"] == "AI title"
    assert catalog.json()[0]["summary"] == "AI summary"
    assert catalog.json()[0]["chapters"][1] == {"start_seconds": 50, "title": "匯率"}

    async with podcast_harness.session_factory() as database:
        analysis_audit = await database.scalar(
            select(AuditEvent).where(AuditEvent.action == "podcast.audio_analyzed")
        )
        assert analysis_audit is not None
        assert analysis_audit.after is not None
        assert analysis_audit.after["metadata_written"] is True
        stored = await database.scalar(
            select(PodcastEpisodeAudioVariant).where(
                PodcastEpisodeAudioVariant.episode_id == uuid.UUID(episode_id)
            )
        )
        assert stored is not None
        assert stored.transcript is not None
        assert stored.transcript["segments"][1]["start"] == 25

    # A manual title survives a re-analysis; the analyze endpoint is async.
    edited = await podcast_harness.admin.put(
        f"/api/admin/podcasts/{episode_id}",
        headers={"X-CSRF-Token": admin_csrf},
        json={
            "expected_version": episode["version"],
            "metadata": {"values": _metadata()},
            "reason": "editor title",
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["metadata_source"] == "manual"
    reanalysis = await podcast_harness.admin.post(
        f"/api/admin/podcasts/{episode_id}/audio/zh-hant/analyze",
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert reanalysis.status_code == 202, reanalysis.text
    assert reanalysis.json()["audio_variants"][0]["analysis_status"] == "pending"
    await podcast_harness.analyzer.wait_for_scheduled()
    final = next(
        item
        for item in (await podcast_harness.admin.get("/api/admin/podcasts")).json()
        if item["id"] == episode_id
    )
    assert final["metadata_source"] == "manual"
    assert final["metadata"][2]["title"] == "Market Brief"
    assert final["audio_variants"][0]["analysis_status"] == "succeeded"
    missing = await podcast_harness.admin.post(
        f"/api/admin/podcasts/{episode_id}/audio/en/analyze",
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert missing.status_code == 404
