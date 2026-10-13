"""Removal exercises real PostgreSQL transactions and an idempotent object store."""

import asyncio
import threading
import uuid
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from test_phase3_podcast_integration import PodcastHarness, _direct_payload, _login, _metadata
from test_phase3_podcast_integration import podcast_harness as podcast_harness
from test_synchronous_podcast_upload import BODY, SHA, complete, final_ref, put, sign

from daily_insights_api.core.enums import AssetKind, AssetStatus
from daily_insights_api.modules.assets.models import Asset
from daily_insights_api.modules.assets.object_store import ObjectRef
from daily_insights_api.modules.audit.models import AuditEvent
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.podcasts.models import (
    PodcastDateGeneration,
    PodcastDeletionJob,
    PodcastDeletionObject,
    PodcastEpisode,
    PodcastEpisodeAudioVariant,
    PodcastEpisodeTranslation,
)

pytestmark = pytest.mark.integration


async def draft(
    harness: PodcastHarness, *, files: int = 0, cover: bool = False
) -> tuple[str, list[ObjectRef]]:
    csrf = await _login(harness.admin, "admin@podcast.test", "AdminPassword123!")
    harness.admin.headers["X-CSRF-Token"] = csrf
    response = await harness.admin.post(
        "/api/admin/podcasts",
        json={
            "trading_date": "2026-10-01",
            "metadata": {"values": _metadata()},
            "reason": "removal test",
        },
    )
    assert response.status_code == 201, response.text
    episode_id = response.json()["id"]
    refs = []
    async with harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, episode_id)
        assert episode is not None
        actor = await database.scalar(select(User).where(User.email == "admin@podcast.test"))
        assert actor is not None
        for index in range(files + int(cover)):
            ref = ObjectRef(bucket="podcast-private", key=f"registered/{index}.mp3")
            refs.append(ref)
            harness.store.put(ref, b"audio", "audio/mpeg", "0" * 64)
            asset = Asset(
                id=uuid.UUID(int=index + 1),
                bucket=ref.bucket,
                object_key=ref.key,
                kind=AssetKind.AUDIO,
                mime_type="audio/mpeg",
                size_bytes=5,
                sha256="0" * 64,
                locale=("zh-hant", "zh-hans", "en")[index % 3],
                localized_titles={},
                status=AssetStatus.ACTIVE if index % 2 == 0 else AssetStatus.ARCHIVED,
                uploaded_by_user_id=actor.id,
            )
            database.add(asset)
            await database.flush()
            if index < files:
                database.add(
                    PodcastEpisodeAudioVariant(
                        episode_id=episode.id,
                        locale=("zh-hant", "zh-hans", "en")[index % 3],
                        version=index + 1,
                        asset_id=asset.id,
                        is_active=index + 3 >= files,
                        activated_by_user_id=actor.id,
                    )
                )
            else:
                episode.cover_asset_id = asset.id
        await database.commit()
    return episode_id, refs


async def remove(client: AsyncClient, episode_id: str, version: int = 1) -> Any:
    return await client.request(
        "DELETE", f"/api/admin/podcasts/{episode_id}", json={"expected_version": version}
    )


async def admin_card(harness: PodcastHarness, episode_id: str) -> dict[str, Any]:
    response = await harness.admin.get("/api/admin/podcasts")
    assert response.status_code == 200
    return next(item for item in response.json() if item["id"] == episode_id)


async def test_removal_roles_csrf_versions_and_never_published_draft(
    podcast_harness: PodcastHarness,
) -> None:
    episode_id, _ = await draft(podcast_harness)
    assert (await remove(podcast_harness.anonymous, episode_id)).status_code == 401
    await _login(podcast_harness.customer, "member@podcast.test", "MemberPassword123!")
    assert (await remove(podcast_harness.customer, episode_id)).status_code == 403
    missing_csrf = await podcast_harness.admin.request(
        "DELETE",
        f"/api/admin/podcasts/{episode_id}",
        headers={"X-CSRF-Token": ""},
        json={"expected_version": 1},
    )
    assert missing_csrf.status_code == 403
    assert (await remove(podcast_harness.admin, episode_id, 9)).status_code == 409
    bad = await podcast_harness.admin.request(
        "DELETE", f"/api/admin/podcasts/{episode_id}", json={"expected_version": 0}
    )
    assert bad.status_code == 422
    csrf = await _login(podcast_harness.asset_manager, "assets@podcast.test", "AssetPassword123!")
    podcast_harness.asset_manager.headers["X-CSRF-Token"] = csrf
    assert (await remove(podcast_harness.asset_manager, episode_id)).status_code == 204
    assert (await remove(podcast_harness.asset_manager, episode_id)).status_code == 204
    assert (await podcast_harness.admin.get("/api/admin/podcasts")).json() == []
    async with podcast_harness.session_factory() as database:
        job = await database.scalar(select(PodcastDeletionJob))
        assert job is not None and job.status == "completed"
        assert await database.scalar(select(PodcastDateGeneration.generation)) == 1
        actions = (
            await database.scalars(
                select(AuditEvent.action).where(AuditEvent.target_id == episode_id)
            )
        ).all()
        assert "podcast.episode_removal_started" in actions and "podcast.episode_removed" in actions


async def test_published_requires_unpublish_and_stale_ticket_cannot_recreate(
    podcast_harness: PodcastHarness,
) -> None:
    csrf, target = await sign(podcast_harness)
    ref = put(podcast_harness, target)
    result = await complete(podcast_harness, csrf, target)
    episode_id = result.json()["episode_id"]
    card = await admin_card(podcast_harness, episode_id)
    response = await remove(podcast_harness.admin, episode_id, card["version"])
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "episode_must_be_unpublished"
    )
    assert ref not in podcast_harness.store.objects
    assert final_ref(podcast_harness, target) in podcast_harness.store.objects
    _, outstanding = await sign(podcast_harness, locale="en")
    outstanding_ref = put(podcast_harness, outstanding)
    unpublish = await podcast_harness.admin.post(
        f"/api/admin/podcasts/{episode_id}/unpublish", json={"expected_version": card["version"]}
    )
    assert unpublish.status_code == 200
    assert (
        await remove(podcast_harness.admin, episode_id, unpublish.json()["version"])
    ).status_code == 204
    for ticket in [target, outstanding]:
        replay = await complete(podcast_harness, csrf, ticket)
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "upload_generation_conflict"
    # Outstanding objects were never registered and are left to existing orphan cleanup.
    assert outstanding_ref in podcast_harness.store.objects
    _, fresh = await sign(podcast_harness)
    put(podcast_harness, fresh)
    recreated = await complete(podcast_harness, csrf, fresh)
    assert recreated.status_code == 200 and recreated.json()["episode_id"] != episode_id
    assert (await complete(podcast_harness, csrf, outstanding)).status_code == 409
    for path, method in [
        (f"/api/podcasts/{episode_id}", "GET"),
        (f"/api/podcasts/{episode_id}/audio-url", "POST"),
    ]:
        assert (await podcast_harness.admin.request(method, path)).status_code == 404
    assert all(
        item["id"] != episode_id
        for item in (await podcast_harness.admin.get("/api/podcasts")).json()
    )


async def test_all_registered_history_and_cover_removed_shared_and_sources_retained(
    podcast_harness: PodcastHarness,
) -> None:
    episode_id, refs = await draft(podcast_harness, files=4, cover=True)
    unrelated = ObjectRef(bucket="source", key="import-original.mp3")
    podcast_harness.store.put(unrelated, b"source", "audio/mpeg")
    async with podcast_harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, episode_id)
        assert episode is not None
        other = PodcastEpisode(
            trading_date=date(2026, 10, 2),
            created_by_user_id=episode.created_by_user_id,
            cover_asset_id=uuid.UUID(int=2),
        )
        database.add(other)
        await database.flush()
        database.add(
            PodcastEpisodeAudioVariant(
                episode_id=other.id,
                locale="en",
                version=1,
                asset_id=uuid.UUID(int=3),
                is_active=True,
                activated_by_user_id=episode.created_by_user_id,
            )
        )
        await database.commit()
    # Missing registered object is successful too.
    podcast_harness.store.objects.pop(refs[0])
    podcast_harness.store.delete_errors[refs[0]] = [
        ClientError({"Error": {"Code": "NoSuchKey"}}, "DeleteObject")
    ]
    assert (await remove(podcast_harness.admin, episode_id)).status_code == 204
    assert set(podcast_harness.store.objects) == {refs[1], refs[2], unrelated}
    async with podcast_harness.session_factory() as database:
        assert await database.get(PodcastEpisode, episode_id) is None
        assert (
            await database.scalar(
                select(func.count())
                .select_from(PodcastEpisodeAudioVariant)
                .where(PodcastEpisodeAudioVariant.episode_id == episode_id)
            )
            == 0
        )
        assert (
            await database.scalar(select(func.count()).select_from(PodcastEpisodeTranslation)) == 0
        )
        assert set((await database.scalars(select(Asset.id))).all()) == {
            uuid.UUID(int=2),
            uuid.UUID(int=3),
        }
        assert (await database.scalars(select(PodcastDeletionObject.status))).all().count(
            "shared"
        ) == 2


async def test_partial_storage_failure_retains_progress_freezes_mutations_and_retries(
    podcast_harness: PodcastHarness,
) -> None:
    episode_id, refs = await draft(podcast_harness, files=3)
    podcast_harness.store.delete_errors[refs[1]] = [
        EndpointConnectionError(endpoint_url="https://r2.test")
    ]
    response = await remove(podcast_harness.admin, episode_id)
    assert (
        response.status_code == 503
        and response.json()["detail"]["code"] == "episode_removal_incomplete"
    )
    card = await admin_card(podcast_harness, episode_id)
    assert card["version"] == 2
    assert card["deletion"] == {
        "status": "pending",
        "total_objects": 3,
        "cleared_objects": 1,
        "retained_objects": 0,
    }
    assert refs[0] not in podcast_harness.store.objects and refs[2] in podcast_harness.store.objects
    operations = [
        ("POST", f"/api/admin/podcasts/{episode_id}/publish", {"expected_version": 2}),
        (
            "PUT",
            f"/api/admin/podcasts/{episode_id}",
            {"expected_version": 2, "metadata": {"values": _metadata()}, "reason": "edit"},
        ),
        (
            "PUT",
            f"/api/admin/podcasts/{episode_id}/audio/en/chapters",
            {"expected_version": 2, "chapters": [], "reason": "edit"},
        ),
        (
            "POST",
            f"/api/admin/podcasts/{episode_id}/audio-imports",
            {
                "source_bucket": "source",
                "source_key": "original.mp3",
                "locale": "en",
                "expected_mime_type": "audio/mpeg",
                "reason": "import",
            },
        ),
    ]
    for method, path, payload in operations:
        frozen = await podcast_harness.admin.request(method, path, json=payload)
        assert (
            frozen.status_code == 409
            and frozen.json()["detail"]["code"] == "episode_removal_pending"
        )
    signing = await podcast_harness.admin.post(
        "/api/admin/podcasts/direct-uploads",
        json={
            "trading_date": "2026-10-01",
            "reason": "initial_upload",
            "files": [
                {
                    "locale": "zh-hant",
                    "filename": "new.mp3",
                    "size_bytes": 5,
                    "mime_type": "audio/mpeg",
                    "sha256": "0" * 64,
                }
            ],
        },
    )
    assert (
        signing.status_code == 409 and signing.json()["detail"]["code"] == "episode_removal_pending"
    )
    assert (await remove(podcast_harness.admin, episode_id)).status_code == 409
    assert (await remove(podcast_harness.admin, episode_id, card["version"])).status_code == 204
    assert podcast_harness.store.objects == {}


@pytest.mark.parametrize(
    "failure_phase", ["object_progress", "final_database", "lost_final_response"]
)
async def test_database_failure_after_storage_deletion_is_recoverable(
    podcast_harness: PodcastHarness, monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    episode_id, refs = await draft(podcast_harness, files=2)
    original = AsyncSession.commit
    failed = False

    async def fail_commit(database: AsyncSession) -> None:
        nonlocal failed
        objects = list(database.identity_map.values())
        match = (
            any(
                isinstance(obj, PodcastDeletionObject) and obj.status == "deleted"
                for obj in objects
            )
            if failure_phase == "object_progress"
            else any(
                isinstance(obj, PodcastDeletionJob) and obj.status == "completed" for obj in objects
            )
        )
        if match and not failed:
            failed = True
            if failure_phase == "lost_final_response":
                await original(database)
            raise OperationalError("injected", {}, Exception("database unavailable"))
        await original(database)

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    result = await remove(podcast_harness.admin, episode_id)
    assert failed and result.status_code == 503
    assert refs[0] not in podcast_harness.store.objects
    if failure_phase == "lost_final_response":
        version = 1
    else:
        card = await admin_card(podcast_harness, episode_id)
        assert card["deletion"] is not None
        version = card["version"]
    assert (await remove(podcast_harness.admin, episode_id, version)).status_code == 204
    assert podcast_harness.store.objects == {}


async def test_remove_serializes_publish_and_upload_completion_without_deadlock(
    podcast_harness: PodcastHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    episode_id, _ = await draft(podcast_harness, files=1)
    csrf, ticket = await sign(podcast_harness, locale="en")
    put(podcast_harness, ticket)
    started = asyncio.Event()
    release = asyncio.Event()
    original_delete = podcast_harness.store.delete

    async def blocked_delete(ref: ObjectRef) -> None:
        started.set()
        await release.wait()
        await original_delete(ref)

    monkeypatch.setattr(podcast_harness.store, "delete", blocked_delete)
    removal = asyncio.create_task(remove(podcast_harness.admin, episode_id))
    await asyncio.wait_for(started.wait(), 5)
    publishing = asyncio.create_task(
        podcast_harness.admin.post(
            f"/api/admin/podcasts/{episode_id}/publish", json={"expected_version": 1}
        )
    )
    completion = asyncio.create_task(complete(podcast_harness, csrf, ticket))
    release.set()
    removed, published, uploaded = await asyncio.wait_for(
        asyncio.gather(removal, publishing, completion), 10
    )
    assert removed.status_code == 204
    assert published.status_code in {404, 409}
    assert uploaded.status_code == 409
    assert uploaded.json()["detail"]["code"] == "upload_generation_conflict"
    assert (await podcast_harness.admin.get("/api/admin/podcasts")).json() == []


async def test_legacy_queued_worker_and_replay_cannot_restore_removed_episode(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
) -> None:
    from daily_insights_api.modules.podcasts.media_worker import PodcastMediaWorker

    episode_id, _ = await draft(podcast_harness)
    payload = _direct_payload(key="queued-before-removal", trading_date="2026-10-01")
    initialized = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches", json=payload
    )
    assert initialized.status_code == 200, initialized.text
    batch = initialized.json()
    upload = batch["files"][0]
    ref = ObjectRef(bucket="podcast-private", key=upload["object_key"])
    podcast_harness.store.put(
        ref, b"nope!", "audio/mpeg", upload["required_headers"]["x-amz-meta-sha256"]
    )
    finalized = await podcast_harness.admin.post(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}/files/zh-hant/finalize"
    )
    assert finalized.status_code == 200
    assert (await remove(podcast_harness.admin, episode_id)).status_code == 204
    assert (
        await podcast_harness.admin.post("/api/admin/podcasts/upload-batches", json=payload)
    ).status_code == 409
    assert (
        await podcast_harness.admin.post(
            f"/api/admin/podcasts/upload-batches/{batch['batch_id']}/files/zh-hant/finalize"
        )
    ).status_code == 409
    worker = PodcastMediaWorker(
        podcast_harness.session_factory,
        podcast_harness.store,
        podcast_harness.settings,
        heartbeat_path=tmp_path / "heartbeat",
        spool_directory=tmp_path,
    )
    assert await worker.process_one()
    assert (await podcast_harness.admin.get("/api/admin/podcasts")).json() == []
    status = await podcast_harness.admin.get(
        f"/api/admin/podcasts/upload-batches/{batch['batch_id']}"
    )
    assert status.json()["status"] == "conflict"
    fresh = await podcast_harness.admin.post(
        "/api/admin/podcasts/upload-batches",
        json={**payload, "idempotency_key": "fresh-after-removal"},
    )
    assert fresh.status_code == 200, fresh.text


@pytest.mark.parametrize("failure", [None, "storage", "database"])
async def test_concurrent_removals_clear_assets_after_last_shared_reference(
    podcast_harness: PodcastHarness,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    first_id, refs = await draft(podcast_harness, files=2)
    async with podcast_harness.session_factory() as database:
        first = await database.get(PodcastEpisode, first_id)
        assert first is not None
        second = PodcastEpisode(
            trading_date=date(2026, 10, 2),
            created_by_user_id=first.created_by_user_id,
            cover_asset_id=uuid.UUID(int=1),
        )
        database.add(second)
        await database.flush()
        database.add(
            PodcastEpisodeAudioVariant(
                episode_id=second.id,
                locale="en",
                version=1,
                asset_id=uuid.UUID(int=2),
                is_active=True,
                activated_by_user_id=first.created_by_user_id,
            )
        )
        await database.commit()
        second_id = str(second.id)
    if failure == "storage":
        podcast_harness.store.delete_errors[refs[0]] = [
            EndpointConnectionError(endpoint_url="https://r2.test")
        ]
    original_commit = AsyncSession.commit
    both_classified = asyncio.Event()
    classifications = 0
    failed = False

    async def coordinated_commit(database: AsyncSession) -> None:
        nonlocal classifications, failed
        classified_last = any(
            isinstance(obj, PodcastDeletionObject)
            and obj.asset_id == uuid.UUID(int=2)
            and obj.status == "shared"
            for obj in database.dirty
        )
        completing_last = any(
            isinstance(obj, PodcastDeletionObject) and obj.status == "deleted"
            for obj in database.identity_map.values()
        ) and any(
            isinstance(obj, PodcastDeletionJob) and obj.status == "completed"
            for obj in database.identity_map.values()
        )
        if failure == "database" and completing_last and not failed:
            failed = True
            raise OperationalError("injected", {}, Exception("shared completion unavailable"))
        await original_commit(database)
        if classified_last:
            classifications += 1
            if classifications == 2:
                both_classified.set()
            await asyncio.wait_for(both_classified.wait(), 5)

    monkeypatch.setattr(AsyncSession, "commit", coordinated_commit)
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            remove(podcast_harness.admin, first_id), remove(podcast_harness.admin, second_id)
        ),
        10,
    )
    if failure:
        assert sorted(outcome.status_code for outcome in outcomes) == [204, 503]
        retained_id = [first_id, second_id][
            next(index for index, outcome in enumerate(outcomes) if outcome.status_code == 503)
        ]
        card = await admin_card(podcast_harness, retained_id)
        assert card["deletion"] is not None and card["version"] == 2
        assert (await remove(podcast_harness.admin, retained_id, 2)).status_code == 204
    else:
        assert [outcome.status_code for outcome in outcomes] == [204, 204]
    async with podcast_harness.session_factory() as database:
        assert (await database.scalars(select(PodcastEpisode))).all() == []
        assert (await database.scalars(select(Asset))).all() == []
        jobs = (await database.scalars(select(PodcastDeletionJob))).all()
        assert all(job.status == "completed" for job in jobs)
    assert all(ref not in podcast_harness.store.objects for ref in refs)


@pytest.mark.parametrize("recreate", [False, True])
async def test_cli_cutover_rejects_frozen_and_stale_verified_imports(
    podcast_harness: PodcastHarness,
    recreate: bool,
) -> None:
    from daily_insights_api.modules.assets.api import AssetMigrationInput
    from daily_insights_api.modules.assets.models import AssetMigrationEntry
    from daily_insights_api.modules.assets.service import (
        AssetMigrationError,
        cutover_migration,
        migrate_podcast_assets,
    )
    from daily_insights_api.scripts import migrate_podcast_assets as migration_cli

    episode_id, refs = await draft(podcast_harness, files=1)
    async with podcast_harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, episode_id)
        assert episode is not None
        actor_id = episode.created_by_user_id
    entry = AssetMigrationInput(
        asset_id=uuid.uuid4(),
        source=ObjectRef(bucket="source", key="pending-import.mp3"),
        target_bucket="podcast-private",
        trading_date=date(2026, 10, 1),
        locale="en",
        expected_mime_type="audio/mpeg",
    )
    podcast_harness.store.put(entry.source, b"new audio", "audio/mpeg")
    migration = await migrate_podcast_assets(podcast_harness.store, (entry,), dry_run=False)
    assert migration.status == "verified"
    async with podcast_harness.session_factory.begin() as database:
        await migration_cli.persist_migration_result(database, migration, actor_user_id=actor_id)
    cutover = await cutover_migration(podcast_harness.store, migration, confirmed=True)
    podcast_harness.store.delete_errors[refs[0]] = [
        EndpointConnectionError(endpoint_url="https://r2.test")
    ]
    assert (await remove(podcast_harness.admin, episode_id)).status_code == 503
    with pytest.raises(AssetMigrationError, match="episode_removal_pending"):
        async with podcast_harness.session_factory.begin() as database:
            await migration_cli.apply_database_cutover(database, cutover, actor_user_id=actor_id)
    async with podcast_harness.session_factory() as database:
        assert (
            await database.scalar(select(func.count()).select_from(PodcastEpisodeAudioVariant)) == 1
        )
        assert await database.get(Asset, entry.asset_id) is None
    assert (await remove(podcast_harness.admin, episode_id, 2)).status_code == 204
    if recreate:
        csrf, fresh = await sign(podcast_harness)
        put(podcast_harness, fresh)
        assert (await complete(podcast_harness, csrf, fresh)).status_code == 200
    # Re-persisting cannot rebind an old verified manifest to a fresh generation.
    for operation, result in [
        (migration_cli.apply_database_cutover, cutover),
        (migration_cli.persist_migration_result, migration),
    ]:
        with pytest.raises(AssetMigrationError, match="migration_generation_conflict"):
            async with podcast_harness.session_factory.begin() as database:
                await operation(database, result, actor_user_id=actor_id)
    async with podcast_harness.session_factory() as database:
        assert await database.get(Asset, entry.asset_id) is None
        assert (
            await database.scalar(
                select(AssetMigrationEntry.podcast_generation).where(
                    AssetMigrationEntry.asset_id == entry.asset_id
                )
            )
            == 0
        )
    assert entry.source in podcast_harness.store.objects
    assert entry.target in podcast_harness.store.objects  # Never registered by rejected cutover.


async def test_cli_multidate_imports_take_sorted_locks_and_do_not_deadlock(
    podcast_harness: PodcastHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.assets.api import AssetMigrationInput
    from daily_insights_api.modules.assets.service import cutover_migration, migrate_podcast_assets
    from daily_insights_api.scripts import migrate_podcast_assets as migration_cli

    episode_id, _ = await draft(podcast_harness)
    async with podcast_harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, episode_id)
        assert episode is not None
        actor_id = episode.created_by_user_id
    cutovers = []
    for locale, days in [("zh-hant", [1, 2]), ("en", [2, 1])]:
        entries = tuple(
            AssetMigrationInput(
                asset_id=uuid.uuid4(),
                source=ObjectRef(bucket="source", key=f"{locale}-{day}.mp3"),
                target_bucket="podcast-private",
                trading_date=date(2026, 10, day),
                locale=locale,
                expected_mime_type="audio/mpeg",
            )
            for day in days
        )
        for entry in entries:
            podcast_harness.store.put(entry.source, b"import", "audio/mpeg")
        verified = await migrate_podcast_assets(podcast_harness.store, entries, dry_run=False)
        async with podcast_harness.session_factory.begin() as database:
            await migration_cli.persist_migration_result(database, verified, actor_user_id=actor_id)
        cutovers.append(await cutover_migration(podcast_harness.store, verified, confirmed=True))
    from daily_insights_api.modules.podcasts.lifecycle import lock_upload_date

    original_lock = lock_upload_date
    acquisitions: list[list[date]] = []

    async def observed_lock(database: AsyncSession, trading_date: date) -> None:
        database.info["dates"].append(trading_date)
        await original_lock(database, trading_date)

    monkeypatch.setattr(migration_cli, "lock_upload_date", observed_lock)

    async def apply(index: int) -> None:
        async with podcast_harness.session_factory.begin() as database:
            sequence: list[date] = []
            acquisitions.append(sequence)
            database.info["dates"] = sequence
            await migration_cli.apply_database_cutover(
                database, cutovers[index], actor_user_id=actor_id
            )

    await asyncio.wait_for(asyncio.gather(apply(0), apply(1)), 10)
    assert acquisitions == [[date(2026, 10, 1), date(2026, 10, 2)]] * 2
    async with podcast_harness.session_factory() as database:
        assert (
            await database.scalar(select(func.count()).select_from(PodcastEpisodeAudioVariant)) == 4
        )


@pytest.mark.parametrize("manifest_status", ["verified", "cutover"])
async def test_public_cli_inventory_replay_cannot_restore_removed_or_replaced_objects(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
    manifest_status: str,
) -> None:
    import json

    from daily_insights_api.modules.assets.api import AssetMigrationInput
    from daily_insights_api.modules.assets.service import AssetMigrationError, cutover_migration
    from daily_insights_api.scripts import migrate_podcast_assets as migration_cli

    episode_id, _ = await draft(podcast_harness)
    async with podcast_harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, episode_id)
        assert episode is not None
        actor_id = episode.created_by_user_id
    entry = AssetMigrationInput(
        asset_id=uuid.uuid4(),
        source=ObjectRef(bucket="source", key="legacy-cli.mp3"),
        target_bucket="podcast-private",
        trading_date=date(2026, 10, 1),
        locale="en",
        expected_mime_type="audio/mpeg",
    )
    podcast_harness.store.put(entry.source, b"legacy audio", "audio/mpeg")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps([entry.model_dump(mode="json")]))
    async with podcast_harness.session_factory.begin() as database:
        verified = await migration_cli.run_migration(
            podcast_harness.store,
            inventory_path=inventory,
            output_path=tmp_path / "verified.json",
            dry_run=False,
            database=database,
            actor_user_id=actor_id,
        )
        repeated = await migration_cli.run_migration(
            podcast_harness.store,
            inventory_path=inventory,
            output_path=tmp_path / "verified-repeat.json",
            dry_run=False,
            database=database,
            actor_user_id=actor_id,
        )
        assert repeated == verified
        if manifest_status == "cutover":
            cutover = await cutover_migration(podcast_harness.store, verified, confirmed=True)
            await migration_cli.apply_database_cutover(database, cutover, actor_user_id=actor_id)
    assert (await remove(podcast_harness.admin, episode_id)).status_code == 204
    if manifest_status == "verified":
        # Unregistered staging objects belong to the existing manual cleanup flow.
        podcast_harness.store.objects.pop(entry.target)
    assert entry.target not in podcast_harness.store.objects
    expected_error = (
        "migration_already_cutover"
        if manifest_status == "cutover"
        else "migration_generation_conflict"
    )
    for recreated in [False, True]:
        if recreated:
            csrf, fresh = await sign(podcast_harness)
            put(podcast_harness, fresh)
            assert (await complete(podcast_harness, csrf, fresh)).status_code == 200
            # Also protect any subsequently staged bytes at the old canonical key.
            podcast_harness.store.put(entry.target, b"replacement recording", "audio/mpeg")
        before = dict(podcast_harness.store.objects)
        output = tmp_path / f"replay-{recreated}.json"
        with pytest.raises(AssetMigrationError, match=expected_error):
            async with podcast_harness.session_factory.begin() as database:
                await migration_cli.run_migration(
                    podcast_harness.store,
                    inventory_path=inventory,
                    output_path=output,
                    dry_run=False,
                    database=database,
                    actor_user_id=actor_id,
                )
        assert podcast_harness.store.objects == before
        assert not output.exists()
        assert entry.source in podcast_harness.store.objects
    async with podcast_harness.session_factory() as database:
        assert await database.get(Asset, entry.asset_id) is None


async def test_public_cli_new_inventory_and_verified_retries_work_after_removal(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
) -> None:
    import json

    from daily_insights_api.modules.assets.api import AssetMigrationInput
    from daily_insights_api.modules.assets.models import AssetMigrationEntry
    from daily_insights_api.scripts import migrate_podcast_assets as migration_cli

    episode_id, _ = await draft(podcast_harness)
    async with podcast_harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, episode_id)
        assert episode is not None
        actor_id = episode.created_by_user_id
    assert (await remove(podcast_harness.admin, episode_id)).status_code == 204
    entry = AssetMigrationInput(
        asset_id=uuid.uuid4(),
        source=ObjectRef(bucket="source", key="fresh-cli.mp3"),
        target_bucket="podcast-private",
        trading_date=date(2026, 10, 1),
        locale="en",
        expected_mime_type="audio/mpeg",
    )
    podcast_harness.store.put(entry.source, b"fresh recording", "audio/mpeg")
    inventory = tmp_path / "fresh-inventory.json"
    inventory.write_text(json.dumps([entry.model_dump(mode="json")]))
    results = []
    for attempt in range(2):
        output = tmp_path / f"fresh-{attempt}.json"
        async with podcast_harness.session_factory.begin() as database:
            results.append(
                await migration_cli.run_migration(
                    podcast_harness.store,
                    inventory_path=inventory,
                    output_path=output,
                    dry_run=False,
                    database=database,
                    actor_user_id=actor_id,
                )
            )
        assert output.exists()
    assert results[0] == results[1] and results[0].status == "verified"
    assert (
        entry.source in podcast_harness.store.objects
        and entry.target in podcast_harness.store.objects
    )
    async with podcast_harness.session_factory() as database:
        assert (
            await database.scalar(
                select(AssetMigrationEntry.podcast_generation).where(
                    AssetMigrationEntry.asset_id == entry.asset_id
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    "inventory_shape", ["subset", "reordered", "mixed", "changed_source", "changed_date"]
)
async def test_public_cli_cross_manifest_entry_replay_has_no_storage_side_effects(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inventory_shape: str,
) -> None:
    import json

    from daily_insights_api.modules.assets.api import AssetMigrationInput
    from daily_insights_api.modules.assets.service import AssetMigrationError
    from daily_insights_api.scripts import migrate_podcast_assets as migration_cli

    episode_id, _ = await draft(podcast_harness)
    async with podcast_harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, episode_id)
        assert episode is not None
        actor_id = episode.created_by_user_id
    entries = [
        AssetMigrationInput(
            asset_id=uuid.uuid4(),
            source=ObjectRef(bucket="source", key=f"owned-{locale}.mp3"),
            target_bucket="podcast-private",
            trading_date=date(2026, 10, 1),
            locale=locale,
            expected_mime_type="audio/mpeg",
        )
        for locale in ("en", "zh-hant")
    ]
    for entry in entries:
        podcast_harness.store.put(entry.source, b"old recording", "audio/mpeg")
    inventory = tmp_path / "original-inventory.json"
    inventory.write_text(json.dumps([entry.model_dump(mode="json") for entry in entries]))
    verified_path = tmp_path / "verified.json"
    async with podcast_harness.session_factory.begin() as database:
        await migration_cli.run_migration(
            podcast_harness.store,
            inventory_path=inventory,
            output_path=verified_path,
            dry_run=False,
            database=database,
            actor_user_id=actor_id,
        )
    async with podcast_harness.session_factory.begin() as database:
        await migration_cli.run_cutover(
            podcast_harness.store,
            database,
            verified_manifest_path=verified_path,
            output_path=tmp_path / "cutover.json",
            source_removal_manifest_path=tmp_path / "source-removal.json",
            actor_user_id=actor_id,
            confirmed=True,
        )
    assert (await remove(podcast_harness.admin, episode_id)).status_code == 204
    assert all(entry.target not in podcast_harness.store.objects for entry in entries)
    fresh = entries[0].model_copy(
        update={
            "asset_id": uuid.uuid4(),
            "source": ObjectRef(bucket="source", key="fresh-mixed.mp3"),
            "locale": "zh-hans",
        }
    )
    changed_source = entries[0].model_copy(
        update={"source": ObjectRef(bucket="source", key="changed-source.mp3")}
    )
    podcast_harness.store.put(fresh.source, b"fresh recording", "audio/mpeg")
    podcast_harness.store.put(changed_source.source, b"changed source recording", "audio/mpeg")
    candidates = {
        "subset": [entries[0]],
        "reordered": list(reversed(entries)),
        "mixed": [fresh, entries[0]],  # Reject before copying even this first fresh entry.
        "changed_source": [changed_source],
        "changed_date": [entries[0].model_copy(update={"trading_date": date(2026, 10, 2)})],
    }[inventory_shape]
    replay = tmp_path / "replay-inventory.json"
    replay.write_text(json.dumps([entry.model_dump(mode="json") for entry in candidates]))
    output = tmp_path / "rejected.json"
    before = dict(podcast_harness.store.objects)
    copy_calls: list[ObjectRef] = []
    original_copy = podcast_harness.store.copy_if_absent

    async def observed_copy(source: ObjectRef, target: ObjectRef, *, sha256: str) -> bool:
        copy_calls.append(target)
        return await original_copy(source, target, sha256=sha256)

    monkeypatch.setattr(podcast_harness.store, "copy_if_absent", observed_copy)
    with pytest.raises(
        AssetMigrationError,
        match=r"migration_already_cutover|migration_entry_owned_by_another_manifest",
    ):
        async with podcast_harness.session_factory.begin() as database:
            await migration_cli.run_migration(
                podcast_harness.store,
                inventory_path=replay,
                output_path=output,
                dry_run=False,
                database=database,
                actor_user_id=actor_id,
            )
    assert copy_calls == []
    assert podcast_harness.store.objects == before
    assert not output.exists()
    assert all(entry.source in podcast_harness.store.objects for entry in entries)


async def test_public_cli_colliding_asset_ownership_serializes_across_dates(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
) -> None:
    import json

    from daily_insights_api.modules.assets.api import AssetMigrationInput
    from daily_insights_api.modules.assets.service import AssetMigrationError
    from daily_insights_api.scripts import migrate_podcast_assets as migration_cli

    episode_id, _ = await draft(podcast_harness)
    async with podcast_harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, episode_id)
        assert episode is not None
        actor_id = episode.created_by_user_id
    asset_id = uuid.uuid4()
    entries = [
        AssetMigrationInput(
            asset_id=asset_id,
            source=ObjectRef(bucket="source", key=f"colliding-{day}.mp3"),
            target_bucket="podcast-private",
            trading_date=date(2026, 10, day),
            locale="en",
            expected_mime_type="audio/mpeg",
        )
        for day in (1, 2)
    ]
    for entry in entries:
        podcast_harness.store.put(entry.source, b"recording", "audio/mpeg")

    async def execute(index: int) -> bool:
        inventory = tmp_path / f"colliding-{index}.json"
        inventory.write_text(json.dumps([entries[index].model_dump(mode="json")]))
        try:
            async with podcast_harness.session_factory.begin() as database:
                await migration_cli.run_migration(
                    podcast_harness.store,
                    inventory_path=inventory,
                    output_path=tmp_path / f"colliding-output-{index}.json",
                    dry_run=False,
                    database=database,
                    actor_user_id=actor_id,
                )
            return True
        except AssetMigrationError as error:
            assert str(error) == "migration_entry_owned_by_another_manifest"
            return False

    outcomes = await asyncio.wait_for(asyncio.gather(execute(0), execute(1)), 10)
    assert sorted(outcomes) == [False, True]
    for index, succeeded in enumerate(outcomes):
        assert (entries[index].target in podcast_harness.store.objects) is succeeded
        assert (tmp_path / f"colliding-output-{index}.json").exists() is succeeded
        assert entries[index].source in podcast_harness.store.objects


async def test_public_cli_refreshes_cached_manifest_before_replay(
    podcast_harness: PodcastHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from daily_insights_api.modules.assets.api import AssetMigrationInput
    from daily_insights_api.modules.assets.models import AssetMigrationEntry, AssetMigrationManifest
    from daily_insights_api.modules.assets.service import AssetMigrationError
    from daily_insights_api.scripts import migrate_podcast_assets as migration_cli

    episode_id, _ = await draft(podcast_harness)
    async with podcast_harness.session_factory() as database:
        episode = await database.get(PodcastEpisode, episode_id)
        assert episode is not None
        actor_id = episode.created_by_user_id
    entry = AssetMigrationInput(
        asset_id=uuid.uuid4(),
        source=ObjectRef(bucket="source", key="cached-manifest.mp3"),
        target_bucket="podcast-private",
        trading_date=date(2026, 10, 1),
        locale="en",
        expected_mime_type="audio/mpeg",
    )
    podcast_harness.store.put(entry.source, b"recording", "audio/mpeg")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps([entry.model_dump(mode="json")]))
    verified_path = tmp_path / "verified.json"
    async with podcast_harness.session_factory() as caller:
        async with caller.begin():
            verified = await migration_cli.run_migration(
                podcast_harness.store,
                inventory_path=inventory,
                output_path=verified_path,
                dry_run=False,
                database=caller,
                actor_user_id=actor_id,
            )
            cached_manifest = await caller.scalar(
                select(AssetMigrationManifest).where(
                    AssetMigrationManifest.idempotency_key == verified.idempotency_key
                )
            )
        assert cached_manifest is not None and cached_manifest.status == "verified"
        async with podcast_harness.session_factory.begin() as other:
            await migration_cli.run_cutover(
                podcast_harness.store,
                other,
                verified_manifest_path=verified_path,
                output_path=tmp_path / "cutover.json",
                source_removal_manifest_path=tmp_path / "source-removal.json",
                actor_user_id=actor_id,
                confirmed=True,
            )
        # Keep a strong reference to the committed caller's stale ORM instance.
        assert cached_manifest.status == "verified"
        before = dict(podcast_harness.store.objects)
        copy_calls: list[ObjectRef] = []
        original_copy = podcast_harness.store.copy_if_absent

        async def observed_copy(source: ObjectRef, target: ObjectRef, *, sha256: str) -> bool:
            copy_calls.append(target)
            return await original_copy(source, target, sha256=sha256)

        monkeypatch.setattr(podcast_harness.store, "copy_if_absent", observed_copy)
        replay_output = tmp_path / "forbidden-replay.json"
        with pytest.raises(AssetMigrationError, match="migration_already_cutover"):
            async with caller.begin():
                await migration_cli.run_migration(
                    podcast_harness.store,
                    inventory_path=inventory,
                    output_path=replay_output,
                    dry_run=False,
                    database=caller,
                    actor_user_id=actor_id,
                )
        assert copy_calls == []
        assert podcast_harness.store.objects == before
        assert not replay_output.exists()
    async with podcast_harness.session_factory() as database:
        manifest = await database.scalar(
            select(AssetMigrationManifest).where(
                AssetMigrationManifest.idempotency_key == verified.idempotency_key
            )
        )
        assert manifest is not None and manifest.status == "cutover"
        assert (
            await database.scalars(
                select(AssetMigrationEntry.status).where(
                    AssetMigrationEntry.manifest_id == manifest.id
                )
            )
        ).all() == ["cutover"]


@pytest.mark.parametrize("phase", ["pending", "shared_final"])
@pytest.mark.parametrize("lost_delete_response", [False, True])
async def test_cancelled_removal_drains_thread_delete_before_retry_and_recreation(
    podcast_harness: PodcastHarness,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    lost_delete_response: bool,
) -> None:
    from daily_insights_api.modules.podcasts import removal
    from daily_insights_api.modules.podcasts.lifecycle import lock_upload_date

    csrf, old = await sign(podcast_harness)
    put(podcast_harness, old)
    uploaded = await complete(podcast_harness, csrf, old)
    assert uploaded.status_code == 200, uploaded.text
    episode_id = uploaded.json()["episode_id"]
    card = await admin_card(podcast_harness, episode_id)
    unpublished = await podcast_harness.admin.post(
        f"/api/admin/podcasts/{episode_id}/unpublish",
        json={"expected_version": card["version"]},
    )
    assert unpublished.status_code == 200
    final = final_ref(podcast_harness, old)
    original_context = ContextVar("podcast_original_removal", default=False)
    retry_context = ContextVar("podcast_removal_retry", default=False)
    classified = asyncio.Event()
    release_classification = asyncio.Event()
    retry_lock = asyncio.Event()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_commit = AsyncSession.commit
    original_delete = podcast_harness.store.delete
    calls = 0

    async def pause_after_classification(database: AsyncSession) -> None:
        sharing = original_context.get() and any(
            isinstance(obj, PodcastDeletionObject) and obj.status == "shared"
            for obj in database.dirty
        )
        await original_commit(database)
        if sharing:
            classified.set()
            await asyncio.wait_for(release_classification.wait(), 5)

    def sdk_delete() -> None:
        started.set()
        try:
            assert release.wait(10), "test did not release the SDK deletion"
            podcast_harness.store.objects.pop(final, None)
            if lost_delete_response:
                raise EndpointConnectionError(endpoint_url="https://r2.test")
        finally:
            finished.set()

    async def delayed_first_delete(ref: ObjectRef) -> None:
        nonlocal calls
        if ref == final:
            calls += 1
            if calls == 1:
                await asyncio.to_thread(sdk_delete)
                return
        await original_delete(ref)

    async def observe_retry_lock(database: AsyncSession, trading_date: date) -> None:
        if retry_context.get():
            retry_lock.set()
        await lock_upload_date(database, trading_date)

    async def run_original() -> Any:
        original_context.set(True)
        return await remove(podcast_harness.admin, episode_id, unpublished.json()["version"])

    async def run_retry(version: int) -> Any:
        retry_context.set(True)
        return await remove(podcast_harness.admin, episode_id, version)

    second_id = None
    if phase == "shared_final":
        # Two real episodes initially share the asset. Pause the first remover
        # after its durable shared classification, then let the second finish.
        # The first must now delete in its final shared-target recheck.
        async with podcast_harness.session_factory() as database:
            first = await database.get(PodcastEpisode, episode_id)
            assert first is not None
            second = PodcastEpisode(
                trading_date=date(2026, 10, 2),
                created_by_user_id=first.created_by_user_id,
                cover_asset_id=uuid.UUID(uploaded.json()["asset_id"]),
            )
            database.add(second)
            await database.commit()
            second_id = str(second.id)
        monkeypatch.setattr(AsyncSession, "commit", pause_after_classification)
    monkeypatch.setattr(podcast_harness.store, "delete", delayed_first_delete)
    monkeypatch.setattr(removal, "lock_upload_date", observe_retry_lock)
    pending = asyncio.create_task(run_original())
    retry: asyncio.Task[Any] | None = None
    try:
        if second_id is not None:
            await asyncio.wait_for(classified.wait(), 5)
            outcome = await remove(podcast_harness.admin, second_id)
            assert outcome.status_code == 204, outcome.text
            assert final in podcast_harness.store.objects
            release_classification.set()
        assert await asyncio.to_thread(started.wait, 5)
        frozen = await admin_card(podcast_harness, episode_id)
        assert frozen["deletion"] is not None
        # Confirm the test reaches each intended storage-deletion branch.
        async with podcast_harness.session_factory() as database:
            status = await database.scalar(
                select(PodcastDeletionObject.status)
                .join(PodcastDeletionJob, PodcastDeletionObject.job_id == PodcastDeletionJob.id)
                .where(PodcastDeletionJob.episode_id == uuid.UUID(episode_id))
            )
            assert status == ("pending" if phase == "pending" else "shared")
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
        retry = asyncio.create_task(run_retry(frozen["version"]))
        await asyncio.wait_for(retry_lock.wait(), 5)
        assert not pending.done(), "cancelled remover released locks during SDK deletion"
        assert not retry.done(), "retry acquired the date before SDK deletion finished"
        assert not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
        outcome = await asyncio.wait_for(retry, 5)
        assert outcome.status_code == 204, outcome.text
        assert finished.is_set()
        _, fresh = await sign(podcast_harness)
        put(podcast_harness, fresh)
        recreated = await complete(podcast_harness, csrf, fresh)
        assert recreated.status_code == 200, recreated.text
        assert recreated.json()["episode_id"] != episode_id
        assert final_ref(podcast_harness, fresh) == final
        assert podcast_harness.store.objects[final] == (BODY, "audio/mpeg", SHA)
        stale = await complete(podcast_harness, csrf, old)
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "upload_generation_conflict"
        assert (
            await remove(podcast_harness.admin, episode_id, frozen["version"])
        ).status_code == 204
        async with podcast_harness.session_factory() as database:
            asset = await database.get(Asset, recreated.json()["asset_id"])
            assert asset is not None and asset.object_key == final.key
        assert final in podcast_harness.store.objects
    finally:
        release_classification.set()
        release.set()
        tasks = [pending] + ([retry] if retry is not None else [])
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        assert await asyncio.to_thread(finished.wait, 5)
