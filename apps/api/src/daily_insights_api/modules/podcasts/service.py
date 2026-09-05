import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, BinaryIO, cast

from mutagen import File as MutagenFile
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
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
    PODCAST_AUDIO_FALLBACK_ORDER,
    Locale,
    PodcastAudioImportRequest,
    PodcastAudioPlaybackResponse,
    PodcastAudioVariant,
    PodcastChapter,
    PodcastEpisodeAdminResponse,
    PodcastEpisodeDetailResponse,
    PodcastEpisodeSummaryResponse,
    PodcastMetadata,
    PodcastMetadataSet,
    resolve_audio_variant,
    validate_chapters,
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


def audio_duration_seconds(content: BinaryIO) -> int | None:
    """Whole seconds of audio in an uploaded file, or None when unreadable.

    The stream is rewound afterwards so the caller can still upload it.
    """
    try:
        content.seek(0)
        parsed = MutagenFile(content)
        info = getattr(parsed, "info", None)
        length = float(getattr(info, "length", 0.0) or 0.0)
    except Exception:
        length = 0.0
    finally:
        content.seek(0)
    return round(length) if length >= 1 else None


def _id3_chapters(content: BinaryIO) -> list[tuple[float, str]]:
    tags = ID3(content)  # type: ignore[no-untyped-call]
    found: list[tuple[float, str]] = []
    for frame in tags.getall("CHAP"):  # type: ignore[no-untyped-call]
        title_frame = frame.sub_frames.get("TIT2")
        title = " ".join(str(item) for item in title_frame.text) if title_frame else ""
        found.append((float(frame.start_time) / 1000.0, title))
    return found


def _mp4_chapters(content: BinaryIO) -> list[tuple[float, str]]:
    parsed = MP4(content)  # type: ignore[no-untyped-call]
    chapters = getattr(parsed, "chapters", None) or []
    return [(float(chapter.start), str(chapter.title or "")) for chapter in chapters]


def audio_chapters(content: BinaryIO, mime_type: str) -> tuple[PodcastChapter, ...]:
    """Chapter markers embedded in an uploaded file (ID3 `CHAP` frames for
    MP3, the chapter track for MP4), or none when the file carries none or
    cannot be read. Untitled or out-of-order markers are dropped rather than
    rejecting the upload; the stream is rewound afterwards."""
    try:
        content.seek(0)
        raw = _mp4_chapters(content) if mime_type == "audio/mp4" else _id3_chapters(content)
    except Exception:
        raw = []
    finally:
        content.seek(0)
    chapters: list[PodcastChapter] = []
    for start, title in sorted(raw, key=lambda item: item[0]):
        cleaned = title.strip()[:120]
        start_seconds = max(0, int(start))
        if not cleaned or (chapters and start_seconds <= chapters[-1].start_seconds):
            continue
        chapters.append(PodcastChapter(start_seconds=start_seconds, title=cleaned))
    try:
        return validate_chapters(tuple(chapters))
    except ValueError:
        return ()


def serialized_chapters(chapters: tuple[PodcastChapter, ...]) -> list[dict[str, Any]]:
    return [chapter.model_dump() for chapter in chapters]


def parsed_chapters(raw: object) -> tuple[PodcastChapter, ...]:
    """Chapters as stored in the JSONB column; anything malformed reads as none."""
    if not isinstance(raw, list):
        return ()
    try:
        return tuple(PodcastChapter.model_validate(item) for item in raw)
    except ValueError:
        return ()


@dataclass(frozen=True)
class ActiveAudioFacts:
    duration_seconds: int | None
    # When the audio file was registered; the client shows it as the
    # episode's release time.
    created_at: datetime
    chapters: tuple[PodcastChapter, ...]


async def active_audio_facts(
    database: AsyncSession, episode_ids: list[uuid.UUID]
) -> dict[tuple[uuid.UUID, str], ActiveAudioFacts]:
    if not episode_ids:
        return {}
    rows = (
        await database.execute(
            select(
                PodcastEpisodeAudioVariant.episode_id,
                PodcastEpisodeAudioVariant.locale,
                PodcastEpisodeAudioVariant.duration_seconds,
                PodcastEpisodeAudioVariant.created_at,
                PodcastEpisodeAudioVariant.chapters,
            ).where(
                PodcastEpisodeAudioVariant.episode_id.in_(episode_ids),
                PodcastEpisodeAudioVariant.is_active.is_(True),
            )
        )
    ).all()
    return {
        (episode_id, locale): ActiveAudioFacts(
            duration_seconds=duration,
            created_at=created_at,
            chapters=parsed_chapters(chapters),
        )
        for episode_id, locale, duration, created_at, chapters in rows
    }


def audio_facts_for(
    facts: dict[tuple[uuid.UUID, str], ActiveAudioFacts], episode_id: uuid.UUID, locale: str
) -> ActiveAudioFacts | None:
    """The requested locale's audio, else the first fallback locale's (the
    player resolves audio the same way)."""
    exact = facts.get((episode_id, locale))
    if exact is not None:
        return exact
    return next(
        (
            facts[(episode_id, candidate)]
            for candidate in PODCAST_AUDIO_FALLBACK_ORDER
            if (episode_id, candidate) in facts
        ),
        None,
    )


def duration_for(
    facts: dict[tuple[uuid.UUID, str], ActiveAudioFacts], episode_id: uuid.UUID, locale: str
) -> int | None:
    """The requested locale's length, else any locale's (the player falls back the same way)."""
    exact = facts.get((episode_id, locale))
    if exact is not None and exact.duration_seconds is not None:
        return exact.duration_seconds
    return next(
        (
            item.duration_seconds
            for (candidate_id, _), item in facts.items()
            if candidate_id == episode_id and item.duration_seconds is not None
        ),
        None,
    )


def audio_created_at_for(
    facts: dict[tuple[uuid.UUID, str], ActiveAudioFacts], episode_id: uuid.UUID, locale: str
) -> datetime | None:
    item = audio_facts_for(facts, episode_id, locale)
    return item.created_at if item is not None else None


def audio_chapters_for(
    facts: dict[tuple[uuid.UUID, str], ActiveAudioFacts], episode_id: uuid.UUID, locale: str
) -> tuple[PodcastChapter, ...]:
    item = audio_facts_for(facts, episode_id, locale)
    return item.chapters if item is not None else ()


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


async def stored_metadata(
    database: AsyncSession, episode_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, PodcastMetadata]]:
    """Title and summary per locale as edited or analysed, keyed by episode."""
    if not episode_ids:
        return {}
    rows = (
        await database.scalars(
            select(PodcastEpisodeTranslation).where(
                PodcastEpisodeTranslation.episode_id.in_(episode_ids)
            )
        )
    ).all()
    result: dict[uuid.UUID, dict[str, PodcastMetadata]] = {}
    for row in rows:
        if row.locale not in ("zh-hant", "zh-hans", "en"):
            continue
        result.setdefault(row.episode_id, {})[row.locale] = PodcastMetadata(
            locale=cast(Locale, row.locale), title=row.title, summary=row.summary
        )
    return result


def metadata_for(
    stored: dict[str, PodcastMetadata] | None, trading_date: date, locale: str
) -> PodcastMetadata:
    """The stored text for the locale when someone (or the analysis) wrote it,
    else the derived "Podcast | date" placeholder."""
    if stored is not None and locale in stored:
        return stored[locale]
    return next(item for item in derived_episode_metadata(trading_date) if item.locale == locale)


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

    stored = (await stored_metadata(database, [episode.id])).get(episode.id)
    return PodcastEpisodeAdminResponse(
        id=episode.id,
        trading_date=episode.trading_date,
        status=episode.status,
        version=episode.version,
        metadata=tuple(
            metadata_for(stored, episode.trading_date, locale)
            for locale in ("zh-hant", "zh-hans", "en")
        ),
        metadata_source=cast(Any, episode.metadata_source),
        audio_variants=tuple(
            PodcastAudioVariantResponse(
                asset_id=item.asset_id,
                locale=item.locale,
                version=item.version,
                is_active=item.is_active,
                duration_seconds=item.duration_seconds,
                chapters=parsed_chapters(item.chapters),
                chapters_source=cast(Any, item.chapters_source),
                analysis_status=cast(Any, item.analysis_status),
                analysis_error=item.analysis_error,
                analyzed_at=item.analyzed_at,
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
    episode_ids = [episode.id for episode in episodes]
    facts = await active_audio_facts(database, episode_ids)
    stored = await stored_metadata(database, episode_ids)
    responses: list[PodcastEpisodeSummaryResponse] = []
    for episode in episodes:
        metadata = metadata_for(stored.get(episode.id), episode.trading_date, locale)
        responses.append(
            PodcastEpisodeSummaryResponse(
                id=episode.id,
                trading_date=episode.trading_date,
                title=metadata.title,
                summary=metadata.summary,
                locale=locale,
                cover_asset_id=episode.cover_asset_id,
                duration_seconds=duration_for(facts, episode.id, locale),
                audio_created_at=audio_created_at_for(facts, episode.id, locale),
                chapters=audio_chapters_for(facts, episode.id, locale),
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
    stored = (await stored_metadata(database, [episode.id])).get(episode.id)
    metadata = metadata_for(stored, episode.trading_date, locale)
    facts = await active_audio_facts(database, [episode.id])
    return PodcastEpisodeDetailResponse(
        id=episode.id,
        trading_date=episode.trading_date,
        title=metadata.title,
        summary=metadata.summary,
        locale=locale,
        cover_asset_id=episode.cover_asset_id,
        duration_seconds=duration_for(facts, episode.id, locale),
        audio_created_at=audio_created_at_for(facts, episode.id, locale),
        chapters=audio_chapters_for(facts, episode.id, locale),
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
        duration_seconds = audio_duration_seconds(upload.content)
        chapters = serialized_chapters(audio_chapters(upload.content, upload.mime_type))
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
            current.duration_seconds = duration_seconds
            # A new recording invalidates the old markers and analysis.
            current.chapters = chapters
            current.chapters_source = "file" if chapters else "none"
            current.analysis_status = "none"
            current.analysis_error = None
            current.analyzed_at = None
            current.transcript = None
            variant = current
        else:
            variant = PodcastEpisodeAudioVariant(
                episode_id=episode.id,
                locale=upload.locale,
                version=version,
                asset_id=asset.id,
                is_active=True,
                activated_by_user_id=actor_user_id,
                duration_seconds=duration_seconds,
                chapters=chapters,
                chapters_source="file" if chapters else "none",
            )
            database.add(variant)
        variants.append(variant)

    episode.version += 1
    await database.flush()
    return tuple(variants)


class PodcastChaptersError(ValueError):
    pass


async def replace_audio_chapters(
    database: AsyncSession,
    episode: PodcastEpisode,
    locale: Locale,
    chapters: tuple[PodcastChapter, ...],
) -> PodcastEpisodeAudioVariant:
    """Overwrite the chapter markers of the active audio for one locale.

    Chapters are navigation aids rather than content, so they may be edited on
    a published episode; the episode version still moves so concurrent
    editors notice each other."""
    variant = await database.scalar(
        select(PodcastEpisodeAudioVariant)
        .where(
            PodcastEpisodeAudioVariant.episode_id == episode.id,
            PodcastEpisodeAudioVariant.locale == locale,
            PodcastEpisodeAudioVariant.is_active.is_(True),
        )
        .with_for_update()
    )
    if variant is None:
        raise PodcastNotFoundError("no audio for this locale")
    try:
        validated = validate_chapters(chapters, duration_seconds=variant.duration_seconds)
    except ValueError as error:
        raise PodcastChaptersError(str(error)) from error
    variant.chapters = serialized_chapters(validated)
    variant.chapters_source = "manual" if validated else "none"
    episode.version += 1
    await database.flush()
    return variant


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
