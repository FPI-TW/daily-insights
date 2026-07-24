import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import AssetKind, AssetStatus
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.assets.api import (
    ALLOWED_PODCAST_AUDIO_MIME_TYPES,
    AssetForSigning,
    AssetMigrationInput,
    AssetMigrationResult,
    MigratedObject,
    SignedAsset,
    migration_idempotency_key,
)
from daily_insights_api.modules.assets.models import Asset
from daily_insights_api.modules.assets.object_store import ObjectMetadata, ObjectStore


class AssetNotSignableError(RuntimeError):
    pass


class AssetMigrationError(RuntimeError):
    pass


async def load_asset_for_signing(
    database: AsyncSession,
    asset_id: uuid.UUID,
) -> AssetForSigning | None:
    asset = await database.scalar(select(Asset).where(Asset.id == asset_id))
    if asset is None or asset.sha256 is None:
        return None
    return AssetForSigning(
        id=asset.id,
        ref={"bucket": asset.bucket, "key": asset.object_key},
        kind=asset.kind,
        status=asset.status,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        sha256=asset.sha256,
    )


async def sign_asset_download(
    store: ObjectStore,
    asset: AssetForSigning,
    *,
    expires_in: timedelta,
) -> SignedAsset:
    if asset.status is not AssetStatus.ACTIVE:
        raise AssetNotSignableError("asset is not active")
    if asset.kind is AssetKind.AUDIO and asset.mime_type not in ALLOWED_PODCAST_AUDIO_MIME_TYPES:
        raise AssetNotSignableError("asset MIME type is not signable")
    if expires_in <= timedelta(0):
        raise ValueError("signed URL lifetime must be positive")

    current = await store.head(asset.ref)
    if current is None:
        raise AssetNotSignableError("asset object is missing")
    if current.size_bytes != asset.size_bytes or current.mime_type != asset.mime_type:
        raise AssetNotSignableError("asset object metadata no longer matches")
    current_sha256 = await _content_sha256(store, current)
    if current_sha256 != asset.sha256:
        raise AssetNotSignableError("asset object checksum no longer matches")

    url = await store.presign_get(asset.ref, expires_in)
    emit_event("asset.signed_url.issued", asset_id=asset.id, status="issued")
    return SignedAsset(
        asset_id=asset.id,
        url=url,
        expires_in_seconds=int(expires_in.total_seconds()),
    )


async def _stream_sha256(chunks: AsyncIterator[bytes]) -> str:
    digest = hashlib.sha256()
    async for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


async def _content_sha256(store: ObjectStore, metadata: ObjectMetadata) -> str:
    if metadata.sha256 is not None:
        normalized = metadata.sha256.lower()
        if len(normalized) == 64 and all(
            character in "0123456789abcdef" for character in normalized
        ):
            return normalized
    return await _stream_sha256(store.read(metadata.ref))


def _validate_source(
    metadata: ObjectMetadata | None,
    entry: AssetMigrationInput,
) -> ObjectMetadata:
    if metadata is None:
        raise AssetMigrationError("source_missing")
    if metadata.size_bytes <= 0:
        raise AssetMigrationError("source_empty")
    if metadata.mime_type != entry.expected_mime_type:
        raise AssetMigrationError("source_mime_mismatch")
    if metadata.mime_type not in ALLOWED_PODCAST_AUDIO_MIME_TYPES:
        raise AssetMigrationError("source_mime_not_allowed")
    return metadata


async def _migrate_one(
    store: ObjectStore,
    entry: AssetMigrationInput,
    *,
    dry_run: bool,
) -> MigratedObject:
    source = _validate_source(await store.head(entry.source), entry)
    source_sha256 = await _stream_sha256(store.read(source.ref))
    target_ref = entry.target

    if dry_run:
        return MigratedObject(
            asset_id=entry.asset_id,
            source=entry.source,
            target=target_ref,
            trading_date=entry.trading_date,
            locale=entry.locale,
            size_bytes=source.size_bytes,
            mime_type=source.mime_type,
            sha256=source_sha256,
            status="planned",
        )

    await store.copy_if_absent(entry.source, target_ref, sha256=source_sha256)
    target = await store.head(target_ref)
    if target is None:
        raise AssetMigrationError("target_missing_after_copy")

    if target.size_bytes != source.size_bytes:
        raise AssetMigrationError("target_size_mismatch")
    if target.mime_type != source.mime_type:
        raise AssetMigrationError("target_mime_mismatch")
    target_sha256 = await _stream_sha256(store.read(target.ref))
    if target_sha256 != source_sha256:
        raise AssetMigrationError("target_sha256_mismatch")

    return MigratedObject(
        asset_id=entry.asset_id,
        source=entry.source,
        target=target_ref,
        trading_date=entry.trading_date,
        locale=entry.locale,
        size_bytes=target.size_bytes,
        mime_type=target.mime_type,
        sha256=target_sha256,
        status="verified",
    )


async def migrate_podcast_assets(
    store: ObjectStore,
    entries: tuple[AssetMigrationInput, ...],
    *,
    dry_run: bool,
) -> AssetMigrationResult:
    if not entries:
        raise ValueError("migration requires at least one object")
    if len({entry.asset_id for entry in entries}) != len(entries):
        raise ValueError("asset IDs must be unique within a migration")
    if len({entry.source for entry in entries}) != len(entries):
        raise ValueError("source objects must be unique within a migration")
    if len({entry.target for entry in entries}) != len(entries):
        raise ValueError("target objects must be unique within a migration")

    migration_id = migration_idempotency_key(entries)
    emit_event(
        "asset.migration.planned",
        migration_id=migration_id,
        status="dry_run" if dry_run else "copying",
    )
    results: list[MigratedObject] = []
    for entry in entries:
        try:
            results.append(await _migrate_one(store, entry, dry_run=dry_run))
        except AssetMigrationError as error:
            results.append(
                MigratedObject(
                    asset_id=entry.asset_id,
                    source=entry.source,
                    target=entry.target,
                    trading_date=entry.trading_date,
                    locale=entry.locale,
                    status="failed",
                    error_code=str(error),
                )
            )

    status = (
        "failed"
        if any(entry.status == "failed" for entry in results)
        else "planned"
        if dry_run
        else "verified"
    )
    if status == "failed":
        emit_event("asset.migration.failed", migration_id=migration_id, status=status)
    elif status == "verified":
        emit_event("asset.migration.verified", migration_id=migration_id, status=status)
    return AssetMigrationResult(
        idempotency_key=migration_id,
        dry_run=dry_run,
        status=status,
        entries=tuple(results),
    )


async def reverify_migration(
    store: ObjectStore,
    migration: AssetMigrationResult,
) -> AssetMigrationResult:
    if migration.dry_run or migration.status != "verified":
        raise AssetMigrationError("migration_not_verified")
    for entry in migration.entries:
        source = await store.head(entry.source)
        target = await store.head(entry.target)
        if source is None or target is None:
            raise AssetMigrationError("cutover_object_missing")
        if (
            source.size_bytes != entry.size_bytes
            or target.size_bytes != entry.size_bytes
            or source.mime_type != entry.mime_type
            or target.mime_type != entry.mime_type
        ):
            raise AssetMigrationError("cutover_metadata_changed")
        source_sha256 = await _stream_sha256(store.read(entry.source))
        target_sha256 = await _stream_sha256(store.read(entry.target))
        if source_sha256 != entry.sha256 or target_sha256 != entry.sha256:
            raise AssetMigrationError("cutover_sha256_changed")
    return migration


async def cutover_migration(
    store: ObjectStore,
    migration: AssetMigrationResult,
    *,
    confirmed: bool,
) -> AssetMigrationResult:
    if not confirmed:
        raise AssetMigrationError("cutover_confirmation_required")
    await reverify_migration(store, migration)
    return migration.model_copy(
        update={
            "status": "cutover",
            "entries": tuple(
                entry.model_copy(update={"status": "cutover"}) for entry in migration.entries
            ),
        }
    )
