import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import AssetKind, AssetStatus
from daily_insights_api.modules.assets.api import (
    ALLOWED_PODCAST_AUDIO_MIME_TYPES,
    Asset,
    AssetMigrationInput,
    ObjectRef,
    ObjectStore,
    canonical_podcast_upload_key,
    load_asset_for_signing,
    migrate_podcast_assets,
    sign_asset_download,
)
from daily_insights_api.modules.podcasts.api import (
    Locale,
    PodcastAudioImportRequest,
    PodcastAudioPlaybackResponse,
    PodcastAudioVariant,
    PodcastEpisodeAdminResponse,
    PodcastEpisodeDetailResponse,
    PodcastEpisodeSummaryResponse,
    PodcastMetadata,
    PodcastMetadataSet,
    resolve_audio_variant,
)
from daily_insights_api.modules.podcasts.models import (
    PodcastEpisode,
    PodcastEpisodeAudioVariant,
    PodcastEpisodeTranslation,
)


class PodcastNotFoundError(LookupError):
    pass


class PodcastConflictError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        current_version: int | None = None,
        current_versions: dict[str, int] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.current_version = current_version
        self.current_versions = current_versions


class PodcastPublicationError(RuntimeError):
    pass


class PodcastMediaUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class PodcastAudioUpload:
    locale: Locale
    content: BinaryIO
    size_bytes: int
    mime_type: str
    sha256: str


def derived_episode_metadata(trading_date: date) -> tuple[PodcastMetadata, ...]:
    values: list[PodcastMetadata] = []
    locales: tuple[Locale, ...] = ("zh-hant", "zh-hans", "en")
    for locale in locales:
        path = PurePosixPath(
            canonical_podcast_upload_key(
                trading_date=trading_date,
                locale=locale,
                mime_type="audio/mpeg",
            )
        )
        day = path.parts[1]
        values.append(
            PodcastMetadata(
                locale=locale,
                title=f"{path.stem.title()} | {day}",
                summary=day,
            )
        )
    return tuple(values)


async def get_episode(
    database: AsyncSession,
    episode_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> PodcastEpisode:
    statement = select(PodcastEpisode).where(PodcastEpisode.id == episode_id)
    if for_update:
        statement = statement.with_for_update()
    episode = await database.scalar(statement)
    if episode is None:
        raise PodcastNotFoundError
    return episode


async def replace_metadata(
    database: AsyncSession,
    episode: PodcastEpisode,
    metadata: PodcastMetadataSet,
) -> None:
    existing = {
        item.locale: item
        for item in (
            await database.scalars(
                select(PodcastEpisodeTranslation).where(
                    PodcastEpisodeTranslation.episode_id == episode.id
                )
            )
        ).all()
    }
    for value in metadata.values:
        translation = existing.get(value.locale)
        if translation is None:
            database.add(
                PodcastEpisodeTranslation(
                    episode_id=episode.id,
                    locale=value.locale,
                    title=value.title.strip(),
                    summary=value.summary.strip(),
                )
            )
        else:
            translation.title = value.title.strip()
            translation.summary = value.summary.strip()


async def episode_admin_response(
    database: AsyncSession,
    episode: PodcastEpisode,
) -> PodcastEpisodeAdminResponse:
    variants = (
        await database.scalars(
            select(PodcastEpisodeAudioVariant)
            .where(PodcastEpisodeAudioVariant.episode_id == episode.id)
            .order_by(
                PodcastEpisodeAudioVariant.locale,
                PodcastEpisodeAudioVariant.version.desc(),
            )
        )
    ).all()
    from daily_insights_api.modules.podcasts.api import PodcastAudioVariantResponse

    return PodcastEpisodeAdminResponse(
        id=episode.id,
        trading_date=episode.trading_date,
        status=episode.status,
        version=episode.version,
        metadata=derived_episode_metadata(episode.trading_date),
        audio_variants=tuple(
            PodcastAudioVariantResponse(
                asset_id=item.asset_id,
                locale=item.locale,
                version=item.version,
                is_active=item.is_active,
            )
            for item in variants
        ),
        cover_asset_id=episode.cover_asset_id,
        published_at=episode.published_at,
    )


async def list_published_episodes(
    database: AsyncSession,
    locale: Locale,
) -> list[PodcastEpisodeSummaryResponse]:
    episodes = (
        await database.scalars(
            select(PodcastEpisode)
            .where(PodcastEpisode.status == "published")
            .order_by(PodcastEpisode.trading_date.desc())
        )
    ).all()
    responses: list[PodcastEpisodeSummaryResponse] = []
    for episode in episodes:
        metadata = next(
            item for item in derived_episode_metadata(episode.trading_date) if item.locale == locale
        )
        responses.append(
            PodcastEpisodeSummaryResponse(
                id=episode.id,
                trading_date=episode.trading_date,
                title=metadata.title,
                summary=metadata.summary,
                locale=locale,
                cover_asset_id=episode.cover_asset_id,
            )
        )
    return responses


async def published_episode_detail(
    database: AsyncSession,
    episode_id: uuid.UUID,
    locale: Locale,
) -> PodcastEpisodeDetailResponse:
    episode = await database.scalar(
        select(PodcastEpisode).where(
            PodcastEpisode.id == episode_id,
            PodcastEpisode.status == "published",
        )
    )
    if episode is None or episode.published_at is None:
        raise PodcastNotFoundError
    metadata = next(
        item for item in derived_episode_metadata(episode.trading_date) if item.locale == locale
    )
    return PodcastEpisodeDetailResponse(
        id=episode.id,
        trading_date=episode.trading_date,
        title=metadata.title,
        summary=metadata.summary,
        locale=locale,
        cover_asset_id=episode.cover_asset_id,
        published_at=episode.published_at,
    )


async def ensure_publishable(database: AsyncSession, episode: PodcastEpisode) -> None:
    audio = await database.scalar(
        select(PodcastEpisodeAudioVariant)
        .join(Asset, Asset.id == PodcastEpisodeAudioVariant.asset_id)
        .where(
            PodcastEpisodeAudioVariant.episode_id == episode.id,
            PodcastEpisodeAudioVariant.is_active.is_(True),
            Asset.status == AssetStatus.ACTIVE,
            Asset.kind == AssetKind.AUDIO,
            Asset.mime_type.in_(ALLOWED_PODCAST_AUDIO_MIME_TYPES),
        )
    )
    if audio is None:
        raise PodcastPublicationError("at least one active audio file is required")


async def upload_audio_batch(
    database: AsyncSession,
    store: ObjectStore,
    settings: Settings,
    episode: PodcastEpisode,
    uploads: tuple[PodcastAudioUpload, ...],
    *,
    actor_user_id: uuid.UUID,
    confirm_replacement: bool,
    expected_versions: dict[str, int],
) -> tuple[PodcastEpisodeAudioVariant, ...]:
    if not 1 <= len(uploads) <= 3:
        raise ValueError("Podcast upload requires one to three files")
    locales = [upload.locale for upload in uploads]
    if len(locales) != len(set(locales)):
        raise ValueError("Podcast upload locales must be unique")
    if settings.r2_bucket_name is None:
        raise PodcastMediaUnavailableError("R2 bucket is not configured")

    current_rows = (
        await database.scalars(
            select(PodcastEpisodeAudioVariant)
            .where(
                PodcastEpisodeAudioVariant.episode_id == episode.id,
                PodcastEpisodeAudioVariant.locale.in_(locales),
                PodcastEpisodeAudioVariant.is_active.is_(True),
            )
            .with_for_update()
        )
    ).all()
    current_by_locale = {row.locale: row for row in current_rows}
    current_asset_ids = [row.asset_id for row in current_rows]
    current_assets = (
        {
            asset.id: asset
            for asset in (
                await database.scalars(
                    select(Asset).where(Asset.id.in_(current_asset_ids)).with_for_update()
                )
            ).all()
        }
        if current_asset_ids
        else {}
    )
    conflicts = {locale: row.version for locale, row in current_by_locale.items()}
    if conflicts and not confirm_replacement:
        raise PodcastConflictError(
            "replacement_confirmation_required",
            current_versions=conflicts,
        )
    for locale, row in current_by_locale.items():
        if expected_versions.get(locale) != row.version:
            raise PodcastConflictError(
                "audio_version_conflict",
                current_versions=conflicts,
            )

    variants: list[PodcastEpisodeAudioVariant] = []
    for upload in uploads:
        current = current_by_locale.get(upload.locale)
        current_asset = current_assets.get(current.asset_id) if current is not None else None
        version = 1 if current is None else current.version + 1
        target = ObjectRef(
            bucket=settings.r2_bucket_name,
            key=canonical_podcast_upload_key(
                trading_date=episode.trading_date,
                locale=upload.locale,
                mime_type=upload.mime_type,
            ),
        )
        await store.overwrite(
            target,
            upload.content,
            size_bytes=upload.size_bytes,
            mime_type=upload.mime_type,
            sha256=upload.sha256,
        )
        asset = current_asset
        if asset is None:
            asset = await database.scalar(
                select(Asset).where(Asset.object_key == target.key).with_for_update()
            )
        if asset is None:
            asset = Asset(
                id=uuid.uuid4(),
                bucket=target.bucket,
                object_key=target.key,
                kind=AssetKind.AUDIO,
                mime_type=upload.mime_type,
                size_bytes=upload.size_bytes,
                sha256=upload.sha256,
                locale=upload.locale,
                localized_titles={},
                status=AssetStatus.ACTIVE,
                uploaded_by_user_id=actor_user_id,
            )
            database.add(asset)
        else:
            previous_ref = ObjectRef(bucket=asset.bucket, key=asset.object_key)
            asset.bucket = target.bucket
            asset.object_key = target.key
            asset.kind = AssetKind.AUDIO
            asset.mime_type = upload.mime_type
            asset.size_bytes = upload.size_bytes
            asset.sha256 = upload.sha256
            asset.locale = upload.locale
            asset.status = AssetStatus.ACTIVE
            asset.uploaded_by_user_id = actor_user_id
            asset.deleted_by_user_id = None
            asset.deleted_at = None
            if previous_ref != target:
                await store.delete(previous_ref)
        if current is not None:
            current.version = version
            current.asset_id = asset.id
            current.activated_by_user_id = actor_user_id
            current.replaced_at = None
            variant = current
        else:
            variant = PodcastEpisodeAudioVariant(
                episode_id=episode.id,
                locale=upload.locale,
                version=version,
                asset_id=asset.id,
                is_active=True,
                activated_by_user_id=actor_user_id,
            )
            database.add(variant)
        variants.append(variant)

    episode.version += 1
    await database.flush()
    return tuple(variants)


async def import_audio(
    database: AsyncSession,
    store: ObjectStore,
    settings: Settings,
    episode: PodcastEpisode,
    payload: PodcastAudioImportRequest,
    *,
    actor_user_id: uuid.UUID,
) -> PodcastEpisodeAudioVariant:
    current = await database.scalar(
        select(PodcastEpisodeAudioVariant)
        .where(
            PodcastEpisodeAudioVariant.episode_id == episode.id,
            PodcastEpisodeAudioVariant.locale == payload.locale,
            PodcastEpisodeAudioVariant.is_active.is_(True),
        )
        .with_for_update()
    )
    if current is not None:
        if not payload.confirm_replacement:
            raise PodcastConflictError(
                "replacement_confirmation_required",
                current_version=current.version,
            )
        if payload.expected_current_version != current.version:
            raise PodcastConflictError(
                "audio_version_conflict",
                current_version=current.version,
            )
    elif payload.expected_current_version is not None:
        raise PodcastConflictError("audio_variant_does_not_exist")

    if settings.r2_bucket_name is None:
        raise PodcastMediaUnavailableError("R2 bucket is not configured")
    asset_id = uuid.uuid4()
    entry = AssetMigrationInput(
        asset_id=asset_id,
        source=ObjectRef(bucket=payload.source_bucket, key=payload.source_key),
        target_bucket=settings.r2_bucket_name,
        trading_date=episode.trading_date,
        locale=payload.locale,
        expected_mime_type=payload.expected_mime_type,
    )
    result = await migrate_podcast_assets(store, (entry,), dry_run=False)
    migrated = result.entries[0]
    if (
        result.status != "verified"
        or migrated.status != "verified"
        or migrated.size_bytes is None
        or migrated.mime_type is None
        or migrated.sha256 is None
    ):
        raise PodcastMediaUnavailableError(migrated.error_code or "audio import failed")

    asset = Asset(
        id=asset_id,
        bucket=migrated.target.bucket,
        object_key=migrated.target.key,
        kind=AssetKind.AUDIO,
        mime_type=migrated.mime_type,
        size_bytes=migrated.size_bytes,
        sha256=migrated.sha256,
        locale=payload.locale,
        localized_titles={},
        status=AssetStatus.ACTIVE,
        uploaded_by_user_id=actor_user_id,
    )
    database.add(asset)
    if current is not None:
        current.is_active = False
        current.replaced_at = datetime.now(UTC)
    variant = PodcastEpisodeAudioVariant(
        episode_id=episode.id,
        locale=payload.locale,
        version=1 if current is None else current.version + 1,
        asset_id=asset_id,
        is_active=True,
        activated_by_user_id=actor_user_id,
    )
    database.add(variant)
    episode.version += 1
    await database.flush()
    return variant


async def sign_episode_audio(
    database: AsyncSession,
    store: ObjectStore,
    settings: Settings,
    episode_id: uuid.UUID,
    requested_locale: Locale,
) -> PodcastAudioPlaybackResponse:
    episode = await database.scalar(
        select(PodcastEpisode).where(
            PodcastEpisode.id == episode_id,
            PodcastEpisode.status == "published",
        )
    )
    if episode is None:
        raise PodcastNotFoundError
    rows = (
        await database.scalars(
            select(PodcastEpisodeAudioVariant).where(
                PodcastEpisodeAudioVariant.episode_id == episode.id,
                PodcastEpisodeAudioVariant.is_active.is_(True),
            )
        )
    ).all()
    resolved = resolve_audio_variant(
        tuple(
            PodcastAudioVariant(
                asset_id=row.asset_id,
                locale=row.locale,
                version=row.version,
            )
            for row in rows
        ),
        requested_locale,
    )
    asset = await load_asset_for_signing(database, resolved.variant.asset_id)
    if asset is None:
        raise PodcastMediaUnavailableError("audio asset is unavailable")
    signed = await sign_asset_download(
        store,
        asset,
        expires_in=timedelta(seconds=settings.r2_signed_url_ttl_seconds),
    )
    return PodcastAudioPlaybackResponse(
        episode_id=episode.id,
        requested_locale=requested_locale,
        resolved_locale=resolved.resolved_locale,
        asset_id=signed.asset_id,
        url=signed.url,
        expires_in_seconds=signed.expires_in_seconds,
    )
