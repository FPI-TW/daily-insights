import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator
from contextvars import ContextVar
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
from daily_insights_api.modules.podcasts.lifecycle import lock_upload_date
from daily_insights_api.modules.podcasts.models import PodcastEpisode, PodcastEpisodeAudioVariant
from daily_insights_api.modules.podcasts.upload_cleanup import cleanup_orphans
from daily_insights_api.modules.podcasts.upload_models import PodcastUploadBatch

pytestmark = pytest.mark.integration
BODY = b"example podcast bytes"
SHA = hashlib.sha256(BODY).hexdigest()


async def sign(
    harness: PodcastHarness,
    *,
    locale: str = "zh-hant",
    version: int | None = None,
    mime: str = "audio/mpeg",
    trading_date: str = "2026-10-01",
) -> tuple[str, dict[str, Any]]:
    csrf = harness.admin.headers.get("X-CSRF-Token")
    if csrf is None:
        csrf = await _login(harness.admin, "admin@podcast.test", "AdminPassword123!")
        harness.admin.headers["X-CSRF-Token"] = csrf
    response = await harness.admin.post(
        "/api/admin/podcasts/direct-uploads",
        headers={"X-CSRF-Token": csrf},
        json={
            "trading_date": trading_date,
            "reason": "initial_upload",
            "files": [
                {
                    "locale": locale,
                    "filename": "podcast.mp3" if mime == "audio/mpeg" else "podcast.mp4",
                    "size_bytes": len(BODY),
                    "mime_type": mime,
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
    harness.store.put(ref, body, ticket.file.mime_type, SHA)
    return ref


def final_ref(harness: PodcastHarness, target: dict[str, Any]) -> ObjectRef:
    ticket = uploads.decode_ticket(target["upload_token"], harness.settings)
    return ObjectRef(bucket="podcast-private", key=ticket.registered_key)


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
    old_staging = put(podcast_harness, initial)
    old_ref = final_ref(podcast_harness, initial)
    assert (await complete(podcast_harness, csrf, initial)).status_code == 200
    _, first = await sign(podcast_harness, version=1)
    _, second = await sign(podcast_harness, version=1)
    first_staging = put(podcast_harness, first)
    first_ref = final_ref(podcast_harness, first)
    second_ref = put(podcast_harness, second)
    assert (await complete(podcast_harness, csrf, first)).status_code == 200
    assert (await complete(podcast_harness, csrf, second)).status_code == 409
    assert first_staging not in podcast_harness.store.objects
    assert old_staging not in podcast_harness.store.objects
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
    assert final_ref(podcast_harness, target) not in podcast_harness.store.objects
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
    assert ref not in podcast_harness.store.objects
    assert final_ref(podcast_harness, target) in podcast_harness.store.objects


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
    staging = put(podcast_harness, target)
    registered = final_ref(podcast_harness, target)
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
    assert staging not in podcast_harness.store.objects
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
    assert final_ref(podcast_harness, target) in podcast_harness.store.objects


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
    original_lock = lock_upload_date

    async def blocked_read(ref: ObjectRef) -> AsyncIterator[bytes]:
        reading.set()
        await release.wait()
        async for chunk in original_read(ref):
            yield chunk

    async def observed_lock(database: AsyncSession, trading_date: Any) -> None:
        cleaning.set()
        await original_lock(database, trading_date)

    monkeypatch.setattr(podcast_harness.store, "read", blocked_read)
    monkeypatch.setattr(upload_cleanup, "lock_upload_date", observed_lock)
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
    # After commit, either the sweep or completion may win the staging-cleanup lock.
    assert count in {0, 1}
    assert ref not in podcast_harness.store.objects
    assert final_ref(podcast_harness, target) in podcast_harness.store.objects


@pytest.mark.parametrize("locale", ["zh-hant", "zh-hans", "en"])
@pytest.mark.parametrize("mime,extension", [("audio/mpeg", "mp3"), ("audio/mp4", "mp4")])
async def test_final_date_locale_and_logical_version(
    podcast_harness: PodcastHarness,
    locale: str,
    mime: str,
    extension: str,
) -> None:
    csrf, target = await sign(podcast_harness, locale=locale, mime=mime, trading_date="2026-02-03")
    staging = put(podcast_harness, target)
    ticket = uploads.decode_ticket(target["upload_token"], podcast_harness.settings)
    assert ticket.version == 2
    assert ticket.registered_key == f"podcasts/2026/02/03/{locale}/podcast_1.{extension}"
    result = await complete(podcast_harness, csrf, target)
    assert result.status_code == 200, result.text
    assert staging not in podcast_harness.store.objects
    async with podcast_harness.session_factory() as database:
        asset = await database.get(Asset, result.json()["asset_id"])
        assert asset is not None and asset.object_key == ticket.registered_key
    _, replacement = await sign(
        podcast_harness, locale=locale, mime=mime, trading_date="2026-02-03", version=1
    )
    put(podcast_harness, replacement)
    assert (await complete(podcast_harness, csrf, replacement)).status_code == 200
    assert final_ref(podcast_harness, target) in podcast_harness.store.objects
    assert final_ref(podcast_harness, replacement).key.endswith(f"podcast_2.{extension}")


async def test_v1_ticket_keeps_legacy_path_then_v2_replaces(
    podcast_harness: PodcastHarness,
) -> None:
    csrf, target = await sign(podcast_harness)
    ticket = uploads.decode_ticket(target["upload_token"], podcast_harness.settings)
    target["upload_token"] = uploads.encode_ticket(
        ticket.model_copy(update={"version": 1}), podcast_harness.settings
    )
    legacy = put(podcast_harness, target)
    assert (await complete(podcast_harness, csrf, target)).status_code == 200
    assert (await complete(podcast_harness, csrf, target)).status_code == 200
    _, replacement = await sign(podcast_harness, version=1)
    put(podcast_harness, replacement)
    assert (await complete(podcast_harness, csrf, replacement)).status_code == 200
    assert legacy in podcast_harness.store.objects
    assert final_ref(podcast_harness, replacement) in podcast_harness.store.objects


@pytest.mark.parametrize(
    "body,mime,sha,status",
    [
        (BODY, "audio/mpeg", SHA, 200),
        (b"x" * len(BODY), "audio/mpeg", SHA, 409),
        (BODY, "video/mp4", SHA, 409),
        (BODY, "audio/mpeg", "0" * 64, 409),
        (b"short", "audio/mpeg", SHA, 409),
    ],
)
async def test_preexisting_final_is_verified_without_overwrite_or_delete(
    podcast_harness: PodcastHarness,
    body: bytes,
    mime: str,
    sha: str,
    status: int,
) -> None:
    csrf, target = await sign(podcast_harness)
    staging = put(podcast_harness, target)
    ref = final_ref(podcast_harness, target)
    podcast_harness.store.put(ref, body, mime, sha)
    before = podcast_harness.store.objects[ref]
    result = await complete(podcast_harness, csrf, target)
    assert result.status_code == status, result.text
    assert podcast_harness.store.objects[ref] == before
    assert staging not in podcast_harness.store.objects


async def test_transient_final_write_retains_staging_and_retries(
    podcast_harness: PodcastHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf, target = await sign(podcast_harness)
    staging = put(podcast_harness, target)
    original = podcast_harness.store.put_if_absent
    calls = 0

    async def fail_once(*args: Any, **kwargs: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            await original(*args, **kwargs)
            raise EndpointConnectionError(endpoint_url="https://r2.test")
        return await original(*args, **kwargs)

    monkeypatch.setattr(podcast_harness.store, "put_if_absent", fail_once)
    assert (await complete(podcast_harness, csrf, target)).status_code == 503
    assert staging in podcast_harness.store.objects
    assert final_ref(podcast_harness, target) in podcast_harness.store.objects
    assert (await complete(podcast_harness, csrf, target)).status_code == 200
    assert staging not in podcast_harness.store.objects


async def test_sweep_only_exact_final_grammar_after_grace(
    podcast_harness: PodcastHarness,
) -> None:
    now = datetime.now(UTC)
    refs = [
        ObjectRef(bucket="podcast-private", key=key)
        for key in [
            "podcasts/2026/02/03/en/podcast_1.mp3",
            "podcasts/2026/02/30/en/podcast_1.mp3",
            "podcasts/2026/2/03/en/podcast_1.mp3",
            "podcasts/2026/02/03/en/podcast_0.mp3",
            "podcasts/2026/02/03/en/podcast_1.mp3.backup",
            "podcasts/2026-02-03/audio/en/podcast.mp3",
            "podcasts/direct/1234567890/2026-02-30/en/00000000-0000-0000-0000-000000000001.mp3",
        ]
    ]
    for ref in refs:
        podcast_harness.store.put(ref, BODY, "audio/mpeg", SHA)
    listing = Listing(podcast_harness.store, now)
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
    assert (
        await cleanup_orphans(
            podcast_harness.session_factory,
            podcast_harness.store,
            listing,
            podcast_harness.settings,
            now=now + timedelta(days=2),
        )
        == 1
    )
    assert refs[0] not in podcast_harness.store.objects
    assert all(ref in podcast_harness.store.objects for ref in refs[1:])


async def test_referenced_unrelated_final_collision_is_never_reused(
    podcast_harness: PodcastHarness,
) -> None:
    import uuid

    from daily_insights_api.core.enums import AssetKind, AssetStatus

    csrf, target = await sign(podcast_harness)
    put(podcast_harness, target)
    ticket = uploads.decode_ticket(target["upload_token"], podcast_harness.settings)
    ref = final_ref(podcast_harness, target)
    podcast_harness.store.put(ref, BODY, "audio/mpeg", SHA)
    async with podcast_harness.session_factory() as database:
        database.add(
            Asset(
                id=uuid.uuid4(),
                bucket=ref.bucket,
                object_key=ref.key,
                kind=AssetKind.AUDIO,
                mime_type="audio/mpeg",
                size_bytes=len(BODY),
                sha256=SHA,
                locale="en",
                localized_titles={},
                status=AssetStatus.ARCHIVED,
                uploaded_by_user_id=ticket.actor_id,
            )
        )
        await database.commit()
    result = await complete(podcast_harness, csrf, target)
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "upload_destination_conflict"
    assert ref in podcast_harness.store.objects
    assert (
        await cleanup_orphans(
            podcast_harness.session_factory,
            podcast_harness.store,
            Listing(podcast_harness.store, datetime.now(UTC)),
            podcast_harness.settings,
            now=datetime.now(UTC) + timedelta(days=2),
        )
        == 0
    )


async def test_rollback_of_reused_final_preserves_unknown_preexisting_object(
    podcast_harness: PodcastHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf, target = await sign(podcast_harness)
    staging = put(podcast_harness, target)
    ref = final_ref(podcast_harness, target)
    podcast_harness.store.put(ref, BODY, "audio/mpeg", SHA)

    async def fail_register(*args: Any, **kwargs: Any) -> uploads.CompletedUpload:
        raise OperationalError("injected", {}, Exception("rollback"))

    monkeypatch.setattr(uploads, "register_audio", fail_register)
    assert (await complete(podcast_harness, csrf, target)).status_code == 503
    assert ref in podcast_harness.store.objects
    assert staging not in podcast_harness.store.objects


async def test_rollback_compensation_does_not_delete_concurrent_winner(
    podcast_harness: PodcastHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf, loser = await sign(podcast_harness)
    _, winner = await sign(podcast_harness)
    loser_staging = put(podcast_harness, loser)
    winner_staging = put(podcast_harness, winner)
    loser_id = uploads.decode_ticket(loser["upload_token"], podcast_harness.settings).asset_id
    registered = asyncio.Event()
    winner_finished = asyncio.Event()
    original_register = uploads.register_audio

    async def fail_loser(
        database: AsyncSession, ticket: uploads.UploadTicket, *args: Any, **kwargs: Any
    ) -> uploads.CompletedUpload:
        result = await original_register(database, ticket, *args, **kwargs)
        if ticket.asset_id == loser_id:
            database.info["compensating_loser"] = True
            registered.set()
            raise OperationalError("injected", {}, Exception("rollback"))
        return result

    async def delay_compensation(database: AsyncSession, trading_date: Any) -> None:
        if database.info.pop("compensating_loser", False):
            await asyncio.wait_for(winner_finished.wait(), 5)
        await lock_upload_date(database, trading_date)

    monkeypatch.setattr(uploads, "register_audio", fail_loser)
    monkeypatch.setattr(uploads, "_lock_upload_date", delay_compensation)
    losing = asyncio.create_task(complete(podcast_harness, csrf, loser))
    await asyncio.wait_for(registered.wait(), 5)
    result = await complete(podcast_harness, csrf, winner)
    winner_finished.set()
    failed = await asyncio.wait_for(losing, 5)
    assert result.status_code == 200 and failed.status_code == 503
    assert final_ref(podcast_harness, winner) in podcast_harness.store.objects
    assert loser_staging not in podcast_harness.store.objects
    assert winner_staging not in podcast_harness.store.objects
    assert (await complete(podcast_harness, csrf, winner)).json() == result.json()


async def test_cleanup_preserves_archived_final_versions(
    podcast_harness: PodcastHarness,
) -> None:
    csrf, original = await sign(podcast_harness)
    put(podcast_harness, original)
    assert (await complete(podcast_harness, csrf, original)).status_code == 200
    _, replacement = await sign(podcast_harness, version=1, mime="audio/mp4")
    put(podcast_harness, replacement)
    assert (await complete(podcast_harness, csrf, replacement)).status_code == 200
    now = datetime.now(UTC)
    assert (
        await cleanup_orphans(
            podcast_harness.session_factory,
            podcast_harness.store,
            Listing(podcast_harness.store, now),
            podcast_harness.settings,
            now=now + timedelta(days=2),
        )
        == 0
    )
    assert final_ref(podcast_harness, original) in podcast_harness.store.objects
    assert final_ref(podcast_harness, replacement) in podcast_harness.store.objects


async def test_cancelled_final_write_drains_before_releasing_stream() -> None:
    # Simulate a SDK thread finishing after caller timeout. The wrapper must keep
    # completion blocked until that write is finished, preserving the lock/spool.
    started = asyncio.Event()
    release = asyncio.Event()
    finished = False

    async def write() -> bool:
        nonlocal finished
        started.set()
        await release.wait()
        finished = True
        return True

    pending = asyncio.create_task(uploads.finish_io(write()))
    await started.wait()
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert finished


async def test_final_sweep_waits_for_registration_and_never_deletes_winner(
    podcast_harness: PodcastHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.podcasts import upload_cleanup

    csrf, target = await sign(podcast_harness)
    put(podcast_harness, target)
    writing = asyncio.Event()
    release = asyncio.Event()
    cleaning = asyncio.Event()
    original_register = uploads.register_audio
    original_lock = lock_upload_date

    async def wait_register(*args: Any, **kwargs: Any) -> uploads.CompletedUpload:
        writing.set()
        await release.wait()
        return await original_register(*args, **kwargs)

    async def observed_lock(database: AsyncSession, trading_date: Any) -> None:
        cleaning.set()
        await original_lock(database, trading_date)

    monkeypatch.setattr(uploads, "register_audio", wait_register)
    monkeypatch.setattr(upload_cleanup, "lock_upload_date", observed_lock)
    completion = asyncio.create_task(complete(podcast_harness, csrf, target))
    await asyncio.wait_for(writing.wait(), 5)
    assert final_ref(podcast_harness, target) in podcast_harness.store.objects
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
    # Staging may be cleared by either participant; final must stay referenced.
    assert count in {0, 1}
    assert final_ref(podcast_harness, target) in podcast_harness.store.objects


async def test_sweep_rechecks_last_modified_after_date_lock(
    podcast_harness: PodcastHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.podcasts import upload_cleanup

    now = datetime.now(UTC)
    ref = ObjectRef(bucket="podcast-private", key="podcasts/2026/02/03/en/podcast_1.mp3")
    podcast_harness.store.put(ref, BODY, "audio/mpeg", SHA)
    listing = Listing(podcast_harness.store, now - timedelta(days=2))
    original_lock = lock_upload_date

    async def refresh(database: AsyncSession, trading_date: Any) -> None:
        await original_lock(database, trading_date)
        listing.modified = now

    monkeypatch.setattr(upload_cleanup, "lock_upload_date", refresh)
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
    assert ref in podcast_harness.store.objects


@pytest.mark.parametrize("owner", ["cleanup", "compensation"])
@pytest.mark.parametrize("lost_delete_response", [False, True])
async def test_cancelled_thread_delete_keeps_date_lock_until_finished(
    podcast_harness: PodcastHarness,
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
    lost_delete_response: bool,
) -> None:
    # Reproduce R2's asyncio.to_thread deletion, including repeated shutdown
    # cancellation and a remote deletion that succeeds before its response fails.
    csrf, loser = await sign(podcast_harness)
    _, winner = await sign(podcast_harness)
    put(podcast_harness, winner)
    final = final_ref(podcast_harness, winner)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    contender_lock = asyncio.Event()
    contender_context = ContextVar("podcast_delete_contender", default=False)
    contender: asyncio.Task[Any] | None = None
    original_delete = podcast_harness.store.delete
    original_register = uploads.register_audio
    loser_id = uploads.decode_ticket(loser["upload_token"], podcast_harness.settings).asset_id

    def sdk_delete() -> None:
        started.set()
        try:
            assert release.wait(10), "test did not release the SDK deletion"
            podcast_harness.store.objects.pop(final, None)
            if lost_delete_response:
                raise EndpointConnectionError(endpoint_url="https://r2.test")
        finally:
            finished.set()

    async def delayed_delete(ref: ObjectRef) -> None:
        if ref == final:
            await asyncio.to_thread(sdk_delete)
        else:
            await original_delete(ref)

    async def observe_lock(database: AsyncSession, trading_date: Any) -> None:
        if contender_context.get():
            contender_lock.set()
        await lock_upload_date(database, trading_date)

    async def run_contender() -> Any:
        contender_context.set(True)
        return await complete(podcast_harness, csrf, winner)

    async def rollback_loser(
        database: AsyncSession, ticket: uploads.UploadTicket, *args: Any, **kwargs: Any
    ) -> uploads.CompletedUpload:
        result = await original_register(database, ticket, *args, **kwargs)
        if ticket.asset_id == loser_id:
            raise OperationalError("injected", {}, Exception("registration rolled back"))
        return result

    monkeypatch.setattr(podcast_harness.store, "delete", delayed_delete)
    monkeypatch.setattr(uploads, "_lock_upload_date", observe_lock)
    if owner == "cleanup":
        podcast_harness.store.put(final, BODY, "audio/mpeg", SHA)
        now = datetime.now(UTC)
        deletion = asyncio.create_task(
            cleanup_orphans(
                podcast_harness.session_factory,
                podcast_harness.store,
                Listing(podcast_harness.store, now - timedelta(days=2)),
                podcast_harness.settings,
                now=now,
            )
        )
    else:
        put(podcast_harness, loser)
        monkeypatch.setattr(uploads, "register_audio", rollback_loser)
        deletion = asyncio.create_task(complete(podcast_harness, csrf, loser))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        deletion.cancel()
        await asyncio.sleep(0)
        deletion.cancel()
        await asyncio.sleep(0)
        contender = asyncio.create_task(run_contender())
        await asyncio.wait_for(contender_lock.wait(), 5)
        assert not deletion.done(), "cancellation released the date lock before SDK completion"
        assert not contender.done(), "contender acquired the date while deletion was in flight"
        assert not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(deletion, 5)
        result = await asyncio.wait_for(contender, 5)
        assert result.status_code == 200, result.text
        assert finished.is_set()
        assert podcast_harness.store.objects[final] == (BODY, "audio/mpeg", SHA)
        async with podcast_harness.session_factory() as database:
            asset = await database.get(Asset, result.json()["asset_id"])
            assert asset is not None and asset.object_key == final.key
        assert (await complete(podcast_harness, csrf, winner)).json() == result.json()
    finally:
        release.set()
        tasks = [deletion] + ([contender] if contender is not None else [])
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        assert await asyncio.to_thread(finished.wait, 5)


async def test_cleanup_skips_unicode_numeric_path_segments(
    podcast_harness: PodcastHarness,
) -> None:
    now = datetime.now(UTC)
    fullwidth = str.maketrans("0123456789", "".join(chr(value) for value in range(0xFF10, 0xFF1A)))
    arabic = str.maketrans("0123456789", "".join(chr(value) for value in range(0x0660, 0x066A)))
    keys = [
        f"podcasts/{'2026'.translate(fullwidth)}/02/03/en/podcast_1.mp3",
        f"podcasts/2026/{'02'.translate(fullwidth)}/03/en/podcast_1.mp3",
        f"podcasts/2026/02/{'03'.translate(fullwidth)}/en/podcast_1.mp3",
        f"podcasts/2026/02/03/en/podcast_1{'2'.translate(fullwidth)}.mp3",
        f"podcasts/{'2026'.translate(arabic)}/02/03/en/podcast_1.mp3",
        f"podcasts/2026/02/03/en/podcast_1{'2'.translate(arabic)}.mp3",
        f"podcasts/direct/{'1234567890'.translate(fullwidth)}/2026-02-03/en/"
        "00000000-0000-0000-0000-000000000001.mp3",
        f"podcasts/direct/1234567890/{'2026'.translate(fullwidth)}-02-03/en/"
        "00000000-0000-0000-0000-000000000001.mp3",
        f"podcasts/direct/1234567890/2026-{'02'.translate(fullwidth)}-03/en/"
        "00000000-0000-0000-0000-000000000001.mp3",
        f"podcasts/direct/1234567890/2026-02-{'03'.translate(fullwidth)}/en/"
        "00000000-0000-0000-0000-000000000001.mp3",
    ]
    refs = [ObjectRef(bucket="podcast-private", key=key) for key in keys]
    for ref in refs:
        podcast_harness.store.put(ref, BODY, "audio/mpeg", SHA)
    assert (
        await cleanup_orphans(
            podcast_harness.session_factory,
            podcast_harness.store,
            Listing(podcast_harness.store, now - timedelta(days=2)),
            podcast_harness.settings,
            now=now,
        )
        == 0
    )
    assert all(ref in podcast_harness.store.objects for ref in refs)
