import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.assets.api import AssetMigrationInput, AssetMigrationResult
from daily_insights_api.modules.assets.object_store import ObjectMetadata, ObjectRef
from daily_insights_api.modules.assets.r2 import R2ObjectStore
from daily_insights_api.modules.assets.service import (
    AssetMigrationError,
    cutover_migration,
    migrate_podcast_assets,
)
from daily_insights_api.scripts import migrate_podcast_assets as migration_cli


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[ObjectRef, tuple[bytes, str, str | None]] = {}
        self.copy_count = 0
        self.corrupt_copies = False

    def put(
        self,
        ref: ObjectRef,
        body: bytes,
        mime_type: str,
        *,
        explicit_sha256: str | None = None,
    ) -> None:
        self.objects[ref] = (body, mime_type, explicit_sha256)

    async def head(self, ref: ObjectRef) -> ObjectMetadata | None:
        value = self.objects.get(ref)
        if value is None:
            return None
        body, mime_type, explicit_sha256 = value
        return ObjectMetadata(
            ref=ref,
            size_bytes=len(body),
            mime_type=mime_type,
            sha256=explicit_sha256,
            etag='"multipart-etag-2"',
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
        self.copy_count += 1
        body, mime_type, explicit_sha256 = self.objects[source]
        if hashlib.sha256(body).hexdigest() != sha256:
            raise ValueError("source body checksum changed before conditional copy")
        if self.corrupt_copies:
            body = b"x" * len(body)
            explicit_sha256 = None
        else:
            explicit_sha256 = sha256
        self.objects[target] = (body, mime_type, explicit_sha256)
        return True

    def read(self, ref: ObjectRef) -> AsyncIterator[bytes]:
        body = self.objects[ref][0]

        async def chunks() -> AsyncIterator[bytes]:
            yield body[:2]
            yield body[2:]

        return chunks()

    async def presign_get(self, ref: ObjectRef, expires_in: timedelta) -> str:
        del ref, expires_in
        raise AssertionError("migration must not sign")


def _entry(source_key: str = "legacy/podcast.mp3") -> AssetMigrationInput:
    return AssetMigrationInput(
        asset_id=uuid.uuid4(),
        source=ObjectRef(bucket="legacy", key=source_key),
        target_bucket="canonical",
        trading_date=date(2026, 7, 24),
        locale="zh-hant",
        expected_mime_type="audio/mpeg",
    )


@pytest.mark.asyncio
async def test_dry_run_reads_and_hashes_but_never_writes() -> None:
    store = FakeObjectStore()
    entry = _entry()
    store.put(entry.source, b"podcast", "audio/mpeg")

    result = await migrate_podcast_assets(store, (entry,), dry_run=True)

    assert result.status == "planned"
    assert result.entries[0].sha256 == hashlib.sha256(b"podcast").hexdigest()
    assert store.copy_count == 0
    assert entry.target not in store.objects


@pytest.mark.asyncio
async def test_repeated_migration_is_idempotent_and_preserves_source() -> None:
    store = FakeObjectStore()
    entry = _entry()
    store.put(entry.source, b"podcast", "audio/mpeg")

    first = await migrate_podcast_assets(store, (entry,), dry_run=False)
    second = await migrate_podcast_assets(store, (entry,), dry_run=False)

    assert first.status == second.status == "verified"
    assert first.idempotency_key == second.idempotency_key
    assert store.copy_count == 1
    assert entry.source in store.objects
    assert entry.target in store.objects


@pytest.mark.asyncio
async def test_source_custom_checksum_is_not_used_as_manifest_proof() -> None:
    store = FakeObjectStore()
    entry = _entry()
    store.put(
        entry.source,
        b"podcast",
        "audio/mpeg",
        explicit_sha256="f" * 64,
    )

    result = await migrate_podcast_assets(store, (entry,), dry_run=False)

    assert result.status == "verified"
    assert result.entries[0].sha256 == hashlib.sha256(b"podcast").hexdigest()
    assert result.entries[0].sha256 != "f" * 64


@pytest.mark.asyncio
async def test_existing_target_self_declared_checksum_cannot_hide_wrong_bytes() -> None:
    store = FakeObjectStore()
    entry = _entry()
    expected = hashlib.sha256(b"podcast").hexdigest()
    store.put(entry.source, b"podcast", "audio/mpeg")
    store.put(entry.target, b"badcast", "audio/mpeg", explicit_sha256=expected)

    result = await migrate_podcast_assets(store, (entry,), dry_run=False)

    assert result.status == "failed"
    assert result.entries[0].error_code == "target_sha256_mismatch"


class ChangingSourceStore(FakeObjectStore):
    async def copy_if_absent(
        self,
        source: ObjectRef,
        target: ObjectRef,
        *,
        sha256: str,
    ) -> bool:
        _, mime_type, metadata_sha256 = self.objects[source]
        self.objects[source] = (b"badcast", mime_type, metadata_sha256)
        return await super().copy_if_absent(source, target, sha256=sha256)


@pytest.mark.asyncio
async def test_source_replacement_between_hash_and_copy_cannot_verify() -> None:
    store = ChangingSourceStore()
    entry = _entry()
    store.put(entry.source, b"podcast", "audio/mpeg", explicit_sha256="f" * 64)

    with pytest.raises(ValueError, match="checksum changed"):
        await migrate_podcast_assets(store, (entry,), dry_run=False)
    assert entry.target not in store.objects


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_body", "target_mime", "error_code"),
    [
        (b"different", "audio/mpeg", "target_size_mismatch"),
        (b"podcast", "audio/wav", "target_mime_mismatch"),
        (b"badcast", "audio/mpeg", "target_sha256_mismatch"),
    ],
)
async def test_conflicting_existing_target_fails_closed(
    target_body: bytes,
    target_mime: str,
    error_code: str,
) -> None:
    store = FakeObjectStore()
    entry = _entry()
    store.put(entry.source, b"podcast", "audio/mpeg")
    store.put(entry.target, target_body, target_mime)

    result = await migrate_podcast_assets(store, (entry,), dry_run=False)

    assert result.status == "failed"
    assert result.entries[0].error_code == error_code
    assert store.copy_count == 0


@pytest.mark.asyncio
async def test_corrupt_copy_fails_checksum_and_cannot_cut_over() -> None:
    store = FakeObjectStore()
    store.corrupt_copies = True
    good = _entry("legacy/good.mp3")
    missing = _entry("legacy/missing.mp3")
    store.put(good.source, b"podcast", "audio/mpeg")

    result = await migrate_podcast_assets(store, (good, missing), dry_run=False)

    assert result.status == "failed"
    assert {entry.error_code for entry in result.entries} == {
        "target_sha256_mismatch",
        "source_missing",
    }
    assert good.source in store.objects
    with pytest.raises(AssetMigrationError, match="migration_not_verified"):
        await cutover_migration(store, result, confirmed=True)


@pytest.mark.asyncio
async def test_verified_manifest_requires_explicit_cutover_confirmation() -> None:
    store = FakeObjectStore()
    entry = _entry()
    store.put(
        entry.source,
        b"podcast",
        "audio/mpeg",
        explicit_sha256=hashlib.sha256(b"podcast").hexdigest(),
    )
    result = await migrate_podcast_assets(store, (entry,), dry_run=False)

    with pytest.raises(AssetMigrationError, match="confirmation"):
        await cutover_migration(store, result, confirmed=False)
    assert (await cutover_migration(store, result, confirmed=True)).status == "cutover"
    assert entry.source in store.objects


def test_cli_builds_default_r2_store_from_secret_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    store = FakeObjectStore()
    store.put(entry.source, b"podcast", "audio/mpeg")
    inventory = tmp_path / "inventory.json"
    output = tmp_path / "result.json"
    inventory.write_text(
        json.dumps([entry.model_dump(mode="json")]),
        encoding="utf-8",
    )
    settings = Settings(
        environment="test",
        r2_endpoint_url="https://account.r2.cloudflarestorage.com",
        r2_bucket_name="private",
        r2_access_key_id=SecretStr("access-id"),
        r2_secret_access_key=SecretStr("secret-key"),
    )
    captured: dict[str, str] = {}

    def factory(**kwargs: str) -> FakeObjectStore:
        captured.update(kwargs)
        return store

    monkeypatch.setattr(migration_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(R2ObjectStore, "from_credentials", staticmethod(factory))

    assert (
        migration_cli.main(["--inventory", str(inventory), "--output", str(output), "--dry-run"])
        == 0
    )
    assert output.is_file()
    assert captured == {
        "endpoint_url": "https://account.r2.cloudflarestorage.com",
        "access_key_id": "access-id",
        "secret_access_key": "secret-key",
    }


@pytest.mark.asyncio
async def test_cli_rejects_unconfirmed_cutover_without_removal_output(
    tmp_path: Path,
) -> None:
    store = FakeObjectStore()
    entry = _entry()
    store.put(entry.source, b"podcast", "audio/mpeg")
    result = await migrate_podcast_assets(store, (entry,), dry_run=False)
    verified = tmp_path / "verified.json"
    output = tmp_path / "cutover.json"
    removal = tmp_path / "removal.json"
    verified.write_text(result.model_dump_json(), encoding="utf-8")
    arguments = migration_cli.parser().parse_args(
        [
            "--verified-manifest",
            str(verified),
            "--output",
            str(output),
            "--source-removal-manifest",
            str(removal),
        ]
    )
    with pytest.raises(SystemExit):
        await migration_cli._execute(arguments, store, Settings(environment="test"))
    assert not output.exists()
    assert not removal.exists()


@pytest.mark.asyncio
async def test_cutover_rejects_stale_target_bytes() -> None:
    store = FakeObjectStore()
    entry = _entry()
    store.put(entry.source, b"podcast", "audio/mpeg")
    verified_result = await migrate_podcast_assets(store, (entry,), dry_run=False)
    store.put(entry.target, b"badcast", "audio/mpeg")
    with pytest.raises(AssetMigrationError, match="sha256_changed"):
        await cutover_migration(store, verified_result, confirmed=True)


def test_forged_verified_manifest_is_rejected_before_cutover() -> None:
    entry = _entry()
    with pytest.raises(ValueError, match="positive size"):
        AssetMigrationResult.model_validate(
            {
                "idempotency_key": "a" * 64,
                "dry_run": False,
                "status": "verified",
                "entries": [
                    {
                        "asset_id": str(entry.asset_id),
                        "source": {"bucket": entry.source.bucket, "key": entry.source.key},
                        "target": {"bucket": entry.target.bucket, "key": entry.target.key},
                        "trading_date": entry.trading_date.isoformat(),
                        "locale": entry.locale,
                        "status": "verified",
                    }
                ],
            }
        )

    store = FakeObjectStore()
    store.put(entry.source, b"podcast", "audio/mpeg")
    valid = asyncio.run(migrate_podcast_assets(store, (entry,), dry_run=False))
    forged = valid.model_dump(mode="json")
    forged["idempotency_key"] = "f" * 64
    with pytest.raises(ValueError, match="idempotency key"):
        AssetMigrationResult.model_validate(forged)
