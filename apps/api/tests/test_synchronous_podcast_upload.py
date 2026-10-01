import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from test_phase3_podcast_integration import (
    FakeObjectStore,
    PodcastHarness,
    _login,
)
from test_phase3_podcast_integration import (
    podcast_harness as podcast_harness,
)

from daily_insights_api.modules.assets.models import Asset
from daily_insights_api.modules.assets.object_store import ListedObject, ObjectRef
from daily_insights_api.modules.podcasts import synchronous_upload as uploads
from daily_insights_api.modules.podcasts.direct_upload import UploadRequest
from daily_insights_api.modules.podcasts.models import PodcastEpisode, PodcastEpisodeAudioVariant
from daily_insights_api.modules.podcasts.upload_cleanup import cleanup_orphans
from daily_insights_api.modules.podcasts.upload_models import PodcastUploadBatch

pytestmark = pytest.mark.integration
BODY = b"example podcast bytes"
SHA = hashlib.sha256(BODY).hexdigest()


async def sign(
    harness: PodcastHarness, *, locale: str = "zh-hant", version: int | None = None
) -> tuple[str, dict[str, Any]]:
    csrf = harness.admin.headers.get("X-CSRF-Token")
    if csrf is None:
        csrf = await _login(harness.admin, "admin@podcast.test", "AdminPassword123!")
        harness.admin.headers["X-CSRF-Token"] = csrf
    response = await harness.admin.post(
        "/api/admin/podcasts/direct-uploads",
        headers={"X-CSRF-Token": csrf},
        json={
            "trading_date": "2026-10-01",
            "reason": "initial_upload",
            "files": [
                {
                    "locale": locale,
                    "filename": "podcast.mp3",
                    "size_bytes": len(BODY),
                    "mime_type": "audio/mpeg",
                    "sha256": SHA,
                    "confirm_replacement": version is not None,
                    "expected_current_version": version,
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    return csrf, response.json()["files"][0]


def put(harness: PodcastHarness, target: dict[str, Any], body: bytes = BODY) -> ObjectRef:
    ticket = uploads.decode_ticket(target["upload_token"], harness.settings)
    ref = ObjectRef(bucket="podcast-private", key=ticket.object_key)
    harness.store.put(ref, body, "audio/mpeg", SHA)
    return ref


async def complete(harness: PodcastHarness, csrf: str, target: dict[str, Any]) -> Any:
    return await harness.admin.post(
        "/api/admin/podcasts/direct-uploads/complete",
        headers={"X-CSRF-Token": csrf},
        json={"upload_token": target["upload_token"]},
    )


async def test_signing_is_stateless_and_old_pending_uploads_do_not_block(
    podcast_harness: PodcastHarness,
) -> None:
    csrf, first = await sign(podcast_harness)
    _, second = await sign(podcast_harness)
    assert first["asset_id"] != second["asset_id"]
    async with podcast_harness.session_factory() as database:
        assert await database.scalar(select(func.count()).select_from(PodcastUploadBatch)) == 0
        assert await database.scalar(select(func.count()).select_from(Asset)) == 0
    missing = await complete(podcast_harness, csrf, first)
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "object_not_uploaded"
    # A legacy browser that refreshed after init left a pending DB session.
    legacy = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        json={
            "idempotency_key": "legacy-pending-upload",
            "trading_date": "2026-10-01",
            "reason": "initial_upload",
            "files": [
                {
                    "locale": "en",
                    "filename": "legacy.mp3",
                    "size_bytes": len(BODY),
                    "mime_type": "audio/mpeg",
                    "sha256": SHA,
                }
            ],
        },
    )
    assert legacy.status_code == 200, legacy.text
    _, second = await sign(podcast_harness)
    put(podcast_harness, second)
    result = await complete(podcast_harness, csrf, second)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed"
    async with podcast_harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, result.json()["episode_id"])
        assert episode is not None and episode.status == "published"


async def test_retry_and_parallel_completion_register_once(
    podcast_harness: PodcastHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf, target = await sign(podcast_harness)
    put(podcast_harness, target)
    results = await asyncio.gather(
        complete(podcast_harness, csrf, target), complete(podcast_harness, csrf, target)
    )
    assert [result.status_code for result in results] == [200, 200]
    assert results[0].json() == results[1].json()

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "ExpiredClock":
            return cls.fromtimestamp((datetime.now(UTC) + timedelta(days=2)).timestamp(), tz)

    monkeypatch.setattr(uploads, "datetime", ExpiredClock)
    assert (await complete(podcast_harness, csrf, target)).json() == results[0].json()
    async with podcast_harness.session_factory() as database:
        assert await database.scalar(select(func.count()).select_from(Asset)) == 1
        assert (
            await database.scalar(select(func.count()).select_from(PodcastEpisodeAudioVariant)) == 1
        )


async def test_checksum_validation_removes_unregistered_object(
    podcast_harness: PodcastHarness,
) -> None:
    csrf, target = await sign(podcast_harness)
    ref = put(podcast_harness, target, b"x" * len(BODY))
    result = await complete(podcast_harness, csrf, target)
    assert result.status_code == 422
    assert ref not in podcast_harness.store.objects
    async with podcast_harness.session_factory() as database:
        assert await database.scalar(select(func.count()).select_from(Asset)) == 0


async def test_owner_signature_csrf_expiry_and_unchanged_size_limit(
    podcast_harness: PodcastHarness,
) -> None:
    csrf, target = await sign(podcast_harness)
    foreign_csrf = await _login(
        podcast_harness.asset_manager, "assets@podcast.test", "AssetPassword123!"
    )
    result = await podcast_harness.asset_manager.post(
        "/api/admin/podcasts/direct-uploads/complete",
        headers={"X-CSRF-Token": foreign_csrf},
        json={"upload_token": target["upload_token"]},
    )
    assert result.status_code == 403
    tampered = dict(target, upload_token=target["upload_token"] + "x")
    assert (await complete(podcast_harness, csrf, tampered)).status_code == 403
    assert (
        await podcast_harness.admin.post(
            "/api/admin/podcasts/direct-uploads/complete",
            headers={"X-CSRF-Token": ""},
            json={"upload_token": target["upload_token"]},
        )
    ).status_code == 403
    ticket = uploads.decode_ticket(target["upload_token"], podcast_harness.settings)
    expired = dict(
        target,
        upload_token=uploads.encode_ticket(
            ticket.model_copy(update={"expires": 1}), podcast_harness.settings
        ),
    )
    assert (await complete(podcast_harness, csrf, expired)).status_code == 410
    assert (
        UploadRequest(
            locale="en",
            filename="large.mp3",
            size_bytes=256 * 1024 * 1024,
            mime_type="audio/mpeg",
            sha256=SHA,
        ).size_bytes
        == 256 * 1024 * 1024
    )


async def test_competing_replacements_keep_winner_and_delete_only_loser(
    podcast_harness: PodcastHarness,
) -> None:
    csrf, initial = await sign(podcast_harness)
    old_ref = put(podcast_harness, initial)
    assert (await complete(podcast_harness, csrf, initial)).status_code == 200
    _, first = await sign(podcast_harness, version=1)
    _, second = await sign(podcast_harness, version=1)
    first_ref = put(podcast_harness, first)
    second_ref = put(podcast_harness, second)
    assert (await complete(podcast_harness, csrf, first)).status_code == 200
    assert (await complete(podcast_harness, csrf, second)).status_code == 409
    assert first_ref in podcast_harness.store.objects
    assert old_ref in podcast_harness.store.objects
    assert second_ref not in podcast_harness.store.objects


async def test_db_rollback_compensates_but_lost_commit_response_recovers(
    podcast_harness: PodcastHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf, target = await sign(podcast_harness)
    ref = put(podcast_harness, target)
    original_register = uploads.register_audio

    async def broken_register(*args: Any, **kwargs: Any) -> uploads.CompletedUpload:
        await original_register(*args, **kwargs)
        raise OperationalError("injected", {}, Exception("database unavailable"))

    monkeypatch.setattr(uploads, "register_audio", broken_register)
    assert (await complete(podcast_harness, csrf, target)).status_code == 503
    assert ref not in podcast_harness.store.objects
    monkeypatch.setattr(uploads, "register_audio", original_register)
    _, target = await sign(podcast_harness)
    ref = put(podcast_harness, target)
    original_commit = AsyncSession.commit

    async def marked_register(
        database: AsyncSession, *args: Any, **kwargs: Any
    ) -> uploads.CompletedUpload:
        result = await original_register(database, *args, **kwargs)
        database.info["lost_commit_response"] = True
        return result

    async def lost_response(database: AsyncSession) -> None:
        await original_commit(database)
        if database.info.pop("lost_commit_response", False):
            raise OperationalError("injected", {}, Exception("commit response lost"))

    monkeypatch.setattr(uploads, "register_audio", marked_register)
    monkeypatch.setattr(AsyncSession, "commit", lost_response)
    result = await complete(podcast_harness, csrf, target)
    assert result.status_code == 200, result.text
    assert ref in podcast_harness.store.objects


class Listing:
    def __init__(self, store: FakeObjectStore, modified: datetime) -> None:
        self.store = store
        self.modified = modified

    async def list_objects(self, bucket: str, prefix: str) -> AsyncIterator[ListedObject]:
        for ref in list(self.store.objects):
            if ref.bucket == bucket and ref.key.startswith(prefix):
                yield ListedObject(ref=ref, last_modified=self.modified)


async def test_sweep_protects_registered_and_recent_objects_and_retries_deletion(
    podcast_harness: PodcastHarness,
) -> None:
    csrf, target = await sign(podcast_harness)
    registered = put(podcast_harness, target)
    assert (await complete(podcast_harness, csrf, target)).status_code == 200
    _, orphan = await sign(podcast_harness, locale="en")
    orphan_ref = put(podcast_harness, orphan)
    now = datetime.now(UTC) + timedelta(days=2)
    listing = Listing(podcast_harness.store, datetime.now(UTC))
    podcast_harness.store.delete_errors[orphan_ref] = [
        EndpointConnectionError(endpoint_url="https://r2.test")
    ]
    assert (
        await cleanup_orphans(
            podcast_harness.session_factory,
            podcast_harness.store,
            listing,
            podcast_harness.settings,
        )
        == 0
    )
    assert (
        await cleanup_orphans(
            podcast_harness.session_factory,
            podcast_harness.store,
            listing,
            podcast_harness.settings,
            now=now,
        )
        == 0
    )
    assert orphan_ref in podcast_harness.store.objects
    assert (
        await cleanup_orphans(
            podcast_harness.session_factory,
            podcast_harness.store,
            listing,
            podcast_harness.settings,
            now=now,
        )
        == 1
    )
    assert registered in podcast_harness.store.objects
    assert orphan_ref not in podcast_harness.store.objects


async def test_transient_r2_read_retains_object_and_retries(
    podcast_harness: PodcastHarness,
) -> None:
    csrf, target = await sign(podcast_harness)
    ref = put(podcast_harness, target)
    podcast_harness.store.read_errors[ref] = [
        EndpointConnectionError(endpoint_url="https://r2.test")
    ]
    assert (await complete(podcast_harness, csrf, target)).status_code == 503
    assert ref in podcast_harness.store.objects
    assert (await complete(podcast_harness, csrf, target)).status_code == 200


async def test_unknown_database_outcome_never_deletes_object(
    podcast_harness: PodcastHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf, target = await sign(podcast_harness)
    ref = put(podcast_harness, target)
    original_lookup = uploads._completed
    failed = False

    async def fail_register(*args: Any, **kwargs: Any) -> uploads.CompletedUpload:
        nonlocal failed
        failed = True
        raise OperationalError("injected", {}, Exception("connection lost"))

    async def unavailable_lookup(*args: Any, **kwargs: Any) -> uploads.CompletedUpload | None:
        if failed:
            raise OperationalError("injected", {}, Exception("database unavailable"))
        return await original_lookup(*args, **kwargs)

    monkeypatch.setattr(uploads, "register_audio", fail_register)
    monkeypatch.setattr(uploads, "_completed", unavailable_lookup)
    assert (await complete(podcast_harness, csrf, target)).status_code == 503
    assert ref in podcast_harness.store.objects


async def test_locales_complete_independently_and_intervening_unpublish_is_preserved(
    podcast_harness: PodcastHarness,
) -> None:
    csrf, first = await sign(podcast_harness)
    _, second = await sign(podcast_harness, locale="en")
    put(podcast_harness, first)
    put(podcast_harness, second)
    initial = await complete(podcast_harness, csrf, first)
    assert initial.status_code == 200
    async with podcast_harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, initial.json()["episode_id"])
        assert episode is not None
        episode.status = "draft"
        episode.published_at = None
        episode.published_by_user_id = None
        episode.version += 1
        await database.commit()
    assert (await complete(podcast_harness, csrf, second)).status_code == 200
    async with podcast_harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, initial.json()["episode_id"])
        assert episode is not None and episode.status == "draft"
        assert (
            await database.scalar(select(func.count()).select_from(PodcastEpisodeAudioVariant)) == 2
        )


async def test_production_rejects_legacy_queue_creation(
    podcast_harness: PodcastHarness,
) -> None:
    # Exercise the production boundary without creating a worker-dependent batch.
    await sign(podcast_harness)
    podcast_harness.settings.environment = "production"
    result = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        json={
            "idempotency_key": "retired-upload-key",
            "trading_date": "2026-10-01",
            "reason": "initial_upload",
            "files": [
                {
                    "locale": "en",
                    "filename": "legacy.mp3",
                    "size_bytes": len(BODY),
                    "mime_type": "audio/mpeg",
                    "sha256": SHA,
                }
            ],
        },
    )
    assert result.status_code == 410
    assert result.json()["detail"]["code"] == "legacy_upload_retired"


async def test_orphan_sweep_waits_for_completion_and_protects_committed_asset(
    podcast_harness: PodcastHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daily_insights_api.modules.podcasts import upload_cleanup

    csrf, target = await sign(podcast_harness)
    ref = put(podcast_harness, target)
    reading = asyncio.Event()
    release = asyncio.Event()
    cleaning = asyncio.Event()
    original_read = podcast_harness.store.read
    original_lock = uploads.lock_upload_object

    async def blocked_read(ref: ObjectRef) -> AsyncIterator[bytes]:
        reading.set()
        await release.wait()
        async for chunk in original_read(ref):
            yield chunk

    async def observed_lock(database: AsyncSession, asset_id: Any) -> None:
        cleaning.set()
        await original_lock(database, asset_id)

    monkeypatch.setattr(podcast_harness.store, "read", blocked_read)
    monkeypatch.setattr(upload_cleanup, "lock_upload_object", observed_lock)
    completion = asyncio.create_task(complete(podcast_harness, csrf, target))
    await asyncio.wait_for(reading.wait(), 5)
    now = datetime.now(UTC)
    sweep = asyncio.create_task(
        cleanup_orphans(
            podcast_harness.session_factory,
            podcast_harness.store,
            Listing(podcast_harness.store, now),
            podcast_harness.settings,
            now=now + timedelta(days=2),
        )
    )
    await asyncio.wait_for(cleaning.wait(), 5)
    assert not sweep.done()
    release.set()
    result, count = await asyncio.wait_for(asyncio.gather(completion, sweep), 5)
    assert result.status_code == 200
    assert count == 0
    assert ref in podcast_harness.store.objects
