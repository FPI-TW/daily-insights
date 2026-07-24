import argparse
import asyncio
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings, get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.enums import AssetKind, AssetStatus, SystemRole, UserStatus
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.assets.api import (
    AssetMigrationInput,
    AssetMigrationResult,
    MigratedObject,
)
from daily_insights_api.modules.assets.models import (
    Asset,
    AssetMigrationEntry,
    AssetMigrationManifest,
)
from daily_insights_api.modules.assets.object_store import ObjectStore
from daily_insights_api.modules.assets.r2 import R2ObjectStore
from daily_insights_api.modules.assets.service import (
    AssetMigrationError,
    cutover_migration,
    migrate_podcast_assets,
)
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.podcasts.models import (
    PodcastEpisode,
    PodcastEpisodeAudioVariant,
)

_INPUT_ADAPTER = TypeAdapter(tuple[AssetMigrationInput, ...])


def parse_inventory(path: Path) -> tuple[AssetMigrationInput, ...]:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    return _INPUT_ADAPTER.validate_python(value)


def parse_execution_manifest(path: Path) -> AssetMigrationResult:
    return AssetMigrationResult.model_validate_json(path.read_text(encoding="utf-8"))


def build_object_store(settings: Settings | None = None) -> ObjectStore:
    resolved = settings or get_settings()
    if (
        resolved.r2_endpoint_url is None
        or resolved.r2_access_key_id is None
        or resolved.r2_secret_access_key is None
    ):
        raise ValueError("complete R2 configuration is required")
    return R2ObjectStore.from_credentials(
        endpoint_url=resolved.r2_endpoint_url,
        access_key_id=resolved.r2_access_key_id.get_secret_value(),
        secret_access_key=resolved.r2_secret_access_key.get_secret_value(),
    )


async def _require_active_admin(database: AsyncSession, actor_user_id: uuid.UUID) -> User:
    actor = await database.scalar(
        select(User).where(
            User.id == actor_user_id,
            User.status == UserStatus.ACTIVE,
            User.system_role == SystemRole.ADMIN,
            User.must_change_password.is_(False),
        )
    )
    if actor is None:
        raise AssetMigrationError("active_admin_required")
    return actor


def _entry_identity(entry: AssetMigrationEntry) -> tuple[object, ...]:
    return (
        entry.asset_id,
        entry.source_bucket,
        entry.source_key,
        entry.target_bucket,
        entry.target_key,
        entry.trading_date,
        entry.locale,
    )


def _result_entry_identity(entry: MigratedObject) -> tuple[object, ...]:
    return (
        entry.asset_id,
        entry.source.bucket,
        entry.source.key,
        entry.target.bucket,
        entry.target.key,
        entry.trading_date,
        entry.locale,
    )


async def persist_migration_result(
    database: AsyncSession,
    result: AssetMigrationResult,
    *,
    actor_user_id: uuid.UUID,
) -> AssetMigrationManifest:
    if result.dry_run:
        raise AssetMigrationError("dry_run_cannot_be_persisted")
    await _require_active_admin(database, actor_user_id)
    manifest = await database.scalar(
        select(AssetMigrationManifest)
        .where(AssetMigrationManifest.idempotency_key == result.idempotency_key)
        .with_for_update()
    )
    if manifest is None:
        manifest = AssetMigrationManifest(
            idempotency_key=result.idempotency_key,
            status=result.status,
            dry_run=False,
            created_by_user_id=actor_user_id,
        )
        database.add(manifest)
        await database.flush()
    elif manifest.status == "cutover":
        raise AssetMigrationError("migration_already_cutover")

    existing_entries = (
        await database.scalars(
            select(AssetMigrationEntry)
            .where(AssetMigrationEntry.manifest_id == manifest.id)
            .with_for_update()
        )
    ).all()
    existing_by_asset = {entry.asset_id: entry for entry in existing_entries}
    if existing_entries and set(existing_by_asset) != {entry.asset_id for entry in result.entries}:
        raise AssetMigrationError("stored_manifest_entry_mismatch")

    for result_entry in result.entries:
        stored = existing_by_asset.get(result_entry.asset_id)
        if stored is None:
            stored = AssetMigrationEntry(
                manifest_id=manifest.id,
                asset_id=result_entry.asset_id,
                trading_date=result_entry.trading_date,
                locale=result_entry.locale,
                source_bucket=result_entry.source.bucket,
                source_key=result_entry.source.key,
                target_bucket=result_entry.target.bucket,
                target_key=result_entry.target.key,
                status=result_entry.status,
            )
            database.add(stored)
        elif _entry_identity(stored) != _result_entry_identity(result_entry):
            raise AssetMigrationError("stored_manifest_entry_mismatch")
        stored.source_size_bytes = result_entry.size_bytes
        stored.target_size_bytes = result_entry.size_bytes
        stored.source_mime_type = result_entry.mime_type
        stored.target_mime_type = result_entry.mime_type
        stored.source_sha256 = result_entry.sha256
        stored.target_sha256 = result_entry.sha256
        stored.status = result_entry.status
        stored.error_code = result_entry.error_code
        stored.error_detail = None
        if result_entry.status == "verified":
            stored.verified_at = datetime.now(UTC)

    manifest.status = result.status
    manifest.error_code = "entry_failed" if result.status == "failed" else None
    manifest.error_detail = None
    await database.flush()
    return manifest


def _asset_matches(asset: Asset, entry: MigratedObject) -> bool:
    return (
        asset.bucket == entry.target.bucket
        and asset.object_key == entry.target.key
        and asset.kind == AssetKind.AUDIO
        and asset.mime_type == entry.mime_type
        and asset.size_bytes == entry.size_bytes
        and asset.sha256 == entry.sha256
        and asset.locale == entry.locale
        and asset.status == AssetStatus.ACTIVE
    )


async def apply_database_cutover(
    database: AsyncSession,
    migration: AssetMigrationResult,
    *,
    actor_user_id: uuid.UUID,
) -> None:
    await _require_active_admin(database, actor_user_id)
    manifest = await database.scalar(
        select(AssetMigrationManifest)
        .where(AssetMigrationManifest.idempotency_key == migration.idempotency_key)
        .with_for_update()
    )
    if manifest is None or manifest.status not in {"verified", "cutover"}:
        raise AssetMigrationError("stored_verified_manifest_required")
    stored_entries = (
        await database.scalars(
            select(AssetMigrationEntry)
            .where(AssetMigrationEntry.manifest_id == manifest.id)
            .with_for_update()
        )
    ).all()
    stored_by_asset = {entry.asset_id: entry for entry in stored_entries}
    if set(stored_by_asset) != {entry.asset_id for entry in migration.entries}:
        raise AssetMigrationError("stored_manifest_entry_mismatch")

    now = datetime.now(UTC)
    for entry in migration.entries:
        stored = stored_by_asset[entry.asset_id]
        if _entry_identity(stored) != _result_entry_identity(entry):
            raise AssetMigrationError("stored_manifest_entry_mismatch")
        if (
            stored.source_size_bytes != entry.size_bytes
            or stored.target_size_bytes != entry.size_bytes
            or stored.source_mime_type != entry.mime_type
            or stored.target_mime_type != entry.mime_type
            or stored.source_sha256 != entry.sha256
            or stored.target_sha256 != entry.sha256
            or stored.status not in {"verified", "cutover"}
        ):
            raise AssetMigrationError("stored_manifest_evidence_mismatch")

        episode = await database.scalar(
            select(PodcastEpisode)
            .where(PodcastEpisode.trading_date == entry.trading_date)
            .with_for_update()
        )
        if episode is None:
            episode = PodcastEpisode(
                trading_date=entry.trading_date,
                status="draft",
                version=1,
                created_by_user_id=actor_user_id,
            )
            database.add(episode)
            await database.flush()
        elif episode.status != "draft":
            raise AssetMigrationError("legacy_import_requires_draft_episode")

        asset = await database.scalar(
            select(Asset).where(Asset.id == entry.asset_id).with_for_update()
        )
        object_owner = await database.scalar(
            select(Asset).where(Asset.object_key == entry.target.key).with_for_update()
        )
        if object_owner is not None and object_owner.id != entry.asset_id:
            raise AssetMigrationError("canonical_target_owned_by_another_asset")
        if asset is None:
            asset = Asset(
                id=entry.asset_id,
                bucket=entry.target.bucket,
                object_key=entry.target.key,
                kind=AssetKind.AUDIO,
                mime_type=entry.mime_type,
                size_bytes=entry.size_bytes,
                sha256=entry.sha256,
                locale=entry.locale,
                localized_titles={},
                status=AssetStatus.ACTIVE,
                uploaded_by_user_id=actor_user_id,
            )
            database.add(asset)
            await database.flush()
        elif not _asset_matches(asset, entry):
            raise AssetMigrationError("existing_asset_mismatch")

        variant = await database.scalar(
            select(PodcastEpisodeAudioVariant)
            .where(
                PodcastEpisodeAudioVariant.episode_id == episode.id,
                PodcastEpisodeAudioVariant.locale == entry.locale,
                PodcastEpisodeAudioVariant.is_active.is_(True),
            )
            .with_for_update()
        )
        if variant is None:
            database.add(
                PodcastEpisodeAudioVariant(
                    episode_id=episode.id,
                    locale=entry.locale,
                    version=1,
                    asset_id=asset.id,
                    is_active=True,
                    activated_by_user_id=actor_user_id,
                )
            )
        elif variant.asset_id != asset.id or variant.version != 1:
            raise AssetMigrationError("podcast_audio_replacement_requires_expected_version")

        stored.status = "cutover"
        stored.cutover_at = stored.cutover_at or now

    manifest.status = "cutover"
    manifest.cutover_by_user_id = actor_user_id
    manifest.cutover_at = manifest.cutover_at or now
    await database.flush()


async def run_migration(
    store: ObjectStore,
    *,
    inventory_path: Path,
    output_path: Path,
    dry_run: bool,
    database: AsyncSession | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> AssetMigrationResult:
    result = await migrate_podcast_assets(
        store,
        parse_inventory(inventory_path),
        dry_run=dry_run,
    )
    if not dry_run:
        if database is None or actor_user_id is None:
            raise AssetMigrationError("database_and_actor_required")
        await persist_migration_result(database, result, actor_user_id=actor_user_id)
    await asyncio.to_thread(
        output_path.write_text,
        result.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return result


async def run_cutover(
    store: ObjectStore,
    database: AsyncSession,
    *,
    verified_manifest_path: Path,
    output_path: Path,
    source_removal_manifest_path: Path,
    actor_user_id: uuid.UUID,
    confirmed: bool,
) -> AssetMigrationResult:
    verified = parse_execution_manifest(verified_manifest_path)
    result = await cutover_migration(store, verified, confirmed=confirmed)
    await apply_database_cutover(database, result, actor_user_id=actor_user_id)
    return result


async def _write_cutover_artifacts(
    result: AssetMigrationResult,
    *,
    output_path: Path,
    source_removal_manifest_path: Path,
) -> None:
    await asyncio.to_thread(
        output_path.write_text,
        result.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    removal_manifest = {
        "migration_id": result.idempotency_key,
        "status": "manual_removal_pending",
        "sources": [
            {"bucket": entry.source.bucket, "key": entry.source.key} for entry in result.entries
        ],
    }
    await asyncio.to_thread(
        source_removal_manifest_path.write_text,
        json.dumps(removal_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Copy, verify, and cut over legacy Podcast objects without deleting sources."
    )
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--inventory", type=Path)
    source.add_argument("--verified-manifest", type=Path)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--actor-user-id", type=uuid.UUID)
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--confirm-cutover", action="store_true")
    command.add_argument("--source-removal-manifest", type=Path)
    return command


async def _execute(arguments: argparse.Namespace, store: ObjectStore, settings: Settings) -> int:
    if arguments.verified_manifest is not None:
        if arguments.dry_run:
            parser().error("--dry-run cannot be used during cutover")
        if not arguments.confirm_cutover:
            parser().error("cutover requires --confirm-cutover")
        if arguments.source_removal_manifest is None:
            parser().error("cutover requires --source-removal-manifest")
        if arguments.actor_user_id is None:
            parser().error("cutover requires --actor-user-id")
        engine = create_engine(settings)
        try:
            session_factory = create_session_factory(engine)
            async with session_factory() as database:
                async with database.begin():
                    result = await run_cutover(
                        store,
                        database,
                        verified_manifest_path=arguments.verified_manifest,
                        output_path=arguments.output,
                        source_removal_manifest_path=arguments.source_removal_manifest,
                        actor_user_id=arguments.actor_user_id,
                        confirmed=True,
                    )
            emit_event(
                "asset.migration.cutover",
                migration_id=result.idempotency_key,
                status="cutover",
            )
            await _write_cutover_artifacts(
                result,
                output_path=arguments.output,
                source_removal_manifest_path=arguments.source_removal_manifest,
            )
            return 0
        finally:
            await engine.dispose()

    if arguments.confirm_cutover or arguments.source_removal_manifest is not None:
        parser().error("cutover options require --verified-manifest")
    assert arguments.inventory is not None
    if arguments.dry_run:
        result = await run_migration(
            store,
            inventory_path=arguments.inventory,
            output_path=arguments.output,
            dry_run=True,
        )
        return 0 if result.status != "failed" else 1
    if arguments.actor_user_id is None:
        parser().error("migration execution requires --actor-user-id")

    engine = create_engine(settings)
    try:
        execution_session_factory = create_session_factory(engine)
        async with execution_session_factory() as database:
            async with database.begin():
                result = await run_migration(
                    store,
                    inventory_path=arguments.inventory,
                    output_path=arguments.output,
                    dry_run=False,
                    database=database,
                    actor_user_id=arguments.actor_user_id,
                )
        return 0 if result.status != "failed" else 1
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None, *, store: ObjectStore | None = None) -> int:
    arguments = parser().parse_args(argv)
    settings = get_settings()
    resolved_store = store or build_object_store(settings)
    return asyncio.run(_execute(arguments, resolved_store, settings))


if __name__ == "__main__":
    raise SystemExit(main())
