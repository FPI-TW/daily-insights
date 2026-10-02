"""Stateless signing and synchronous, idempotent Podcast registration."""

import asyncio
import base64
import hashlib
import hmac
import logging
import tempfile
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import AssetKind, AssetStatus
from daily_insights_api.modules.assets.api import Asset, ObjectRef, ObjectStore
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.podcasts.direct_upload import (
    MAX_PODCAST_AUDIO_BYTES,
    AssetWrite,
    Database,
    Locale,
    Store,
    UploadBatchRequest,
    UploadReason,
    UploadRequest,
    _lock_upload_date,
)
from daily_insights_api.modules.podcasts.direct_upload import (
    router as router,
)
from daily_insights_api.modules.podcasts.media_worker import OBJECT_STORE_ERRORS
from daily_insights_api.modules.podcasts.models import PodcastEpisode, PodcastEpisodeAudioVariant
from daily_insights_api.modules.podcasts.service import (
    audio_chapters,
    audio_duration_seconds,
    serialized_chapters,
)

logger = logging.getLogger(__name__)
UPLOAD_PREFIX = "podcasts/direct/"


class DirectUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trading_date: date
    reason: UploadReason
    files: tuple[UploadRequest, ...] = Field(min_length=1, max_length=3)


class UploadTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    actor_id: uuid.UUID
    asset_id: uuid.UUID
    trading_date: date
    reason: UploadReason
    file: UploadRequest
    expires: int
    episode_version: int | None
    began_published: bool

    @property
    def object_key(self) -> str:
        extension = "mp3" if self.file.mime_type == "audio/mpeg" else "mp4"
        return (
            f"{UPLOAD_PREFIX}{self.expires}/{self.trading_date}/"
            f"{self.file.locale}/{self.asset_id}.{extension}"
        )


class DirectUploadTarget(BaseModel):
    asset_id: uuid.UUID
    locale: Locale
    upload_url: str
    upload_token: str
    required_headers: dict[str, str]
    expires_at: datetime


class DirectUploadResponse(BaseModel):
    files: list[DirectUploadTarget]


class CompleteUploadRequest(BaseModel):
    upload_token: str = Field(min_length=1, max_length=8192)


class CompletedUpload(BaseModel):
    asset_id: uuid.UUID
    episode_id: uuid.UUID
    locale: Locale
    status: Literal["completed"] = "completed"
    sha256: str
    duration_seconds: int | None


def _signature(settings: Settings, payload: str) -> str:
    assert settings.session_secret is not None
    return hmac.new(
        settings.session_secret.get_secret_value().encode(),
        b"podcast-direct-upload-v1:" + payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def encode_ticket(ticket: UploadTicket, settings: Settings) -> str:
    payload = base64.urlsafe_b64encode(ticket.model_dump_json().encode()).decode()
    return f"{payload}.{_signature(settings, payload)}"


def decode_ticket(token: str, settings: Settings) -> UploadTicket:
    try:
        payload, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, _signature(settings, payload)):
            raise ValueError("signature")
        return UploadTicket.model_validate_json(
            base64.b64decode(payload, altchars=b"-_", validate=True)
        )
    except (ValueError, ValidationError) as error:
        raise HTTPException(403, detail={"code": "invalid_upload_token"}) from error


async def lock_upload_object(database: AsyncSession, asset_id: uuid.UUID) -> None:
    # Shared by completion, compensation and orphan cleanup, across API replicas.
    key = int.from_bytes(hashlib.sha256(asset_id.bytes).digest()[:8], "big", signed=True)
    await database.execute(select(func.pg_advisory_xact_lock(key)))


async def _completed(database: AsyncSession, ticket: UploadTicket) -> CompletedUpload | None:
    row = (
        await database.execute(
            select(Asset, PodcastEpisodeAudioVariant)
            .join(PodcastEpisodeAudioVariant, PodcastEpisodeAudioVariant.asset_id == Asset.id)
            .where(Asset.id == ticket.asset_id)
        )
    ).one_or_none()
    if row is None:
        return None
    asset, variant = row
    if asset.object_key != ticket.object_key or asset.uploaded_by_user_id != ticket.actor_id:
        raise HTTPException(409, detail={"code": "upload_identity_conflict"})
    return CompletedUpload(
        asset_id=asset.id,
        episode_id=variant.episode_id,
        locale=ticket.file.locale,
        sha256=asset.sha256,
        duration_seconds=variant.duration_seconds,
    )


async def delete_unreferenced(
    database: AsyncSession,
    store: ObjectStore,
    ref: ObjectRef,
) -> bool:
    referenced = await database.scalar(select(Asset.id).where(Asset.object_key == ref.key))
    if referenced is not None:
        return False
    try:
        await store.delete(ref)
    except OBJECT_STORE_ERRORS:
        logger.warning("Podcast object deletion deferred to orphan cleanup")
        return False
    return True


@router.post(
    "/api/admin/podcasts/direct-uploads",
    response_model=DirectUploadResponse,
    operation_id="admin_podcast_direct_upload_sign",
)
async def sign_direct_upload(
    payload: DirectUploadRequest,
    actor: AssetWrite,
    database: Database,
    store: Store,
    request: Request,
) -> DirectUploadResponse:
    settings: Settings = request.app.state.settings
    if settings.r2_bucket_name is None:
        raise HTTPException(503, detail={"code": "r2_not_configured"})
    files = UploadBatchRequest(
        idempotency_key="stateless-upload-validation", **payload.model_dump()
    ).validated_files()
    episode = await database.scalar(
        select(PodcastEpisode).where(PodcastEpisode.trading_date == payload.trading_date)
    )
    versions: dict[str, int] = {}
    if episode is not None:
        variants = await database.scalars(
            select(PodcastEpisodeAudioVariant).where(
                PodcastEpisodeAudioVariant.episode_id == episode.id,
                PodcastEpisodeAudioVariant.is_active.is_(True),
            )
        )
        versions = {variant.locale: variant.version for variant in variants}
    replacements = {
        item.locale: versions[item.locale]
        for item, _, _ in files
        if item.locale in versions and not item.confirm_replacement
    }
    if replacements:
        raise HTTPException(
            409,
            detail={
                "code": "replacement_confirmation_required",
                "current_versions": replacements,
            },
        )
    mismatches = {
        item.locale: versions.get(item.locale)
        for item, _, _ in files
        if item.expected_current_version != versions.get(item.locale)
        or (item.confirm_replacement and item.locale not in versions)
    }
    if mismatches:
        raise HTTPException(
            409, detail={"code": "audio_version_conflict", "current_versions": mismatches}
        )
    expires = int(datetime.now(UTC).timestamp()) + settings.podcast_upload_presign_ttl_seconds
    targets = []
    for item, _, mime in files:
        ticket = UploadTicket(
            actor_id=actor.user.id,
            asset_id=uuid.uuid4(),
            trading_date=payload.trading_date,
            reason=payload.reason,
            file=item.model_copy(update={"mime_type": mime}),
            expires=expires,
            episode_version=episode.version if episode else None,
            began_published=episode.status == "published" if episode else False,
        )
        url = await store.presign_put(
            ObjectRef(bucket=settings.r2_bucket_name, key=ticket.object_key),
            mime_type=mime,
            sha256=item.sha256,
            expires_in=timedelta(seconds=settings.podcast_upload_presign_ttl_seconds),
        )
        targets.append(
            DirectUploadTarget(
                asset_id=ticket.asset_id,
                locale=item.locale,
                upload_url=url,
                upload_token=encode_ticket(ticket, settings),
                required_headers={
                    "Content-Type": mime,
                    "If-None-Match": "*",
                    "x-amz-meta-sha256": item.sha256,
                },
                expires_at=datetime.fromtimestamp(expires, UTC),
            )
        )
    return DirectUploadResponse(files=targets)


async def verify_audio(
    store: ObjectStore, ref: ObjectRef, ticket: UploadTicket, *, spool_dir: str | None = None
) -> tuple[int | None, list[dict[str, object]]]:
    metadata = await store.head(ref)
    if metadata is None:
        raise HTTPException(409, detail={"code": "object_not_uploaded"})
    expected = ticket.file
    if (
        metadata.size_bytes != expected.size_bytes
        or metadata.mime_type.lower() != expected.mime_type
        or metadata.sha256 != expected.sha256
    ):
        raise HTTPException(422, detail={"code": "uploaded_metadata_mismatch"})
    digest = hashlib.sha256()
    size = 0
    # Bound memory independently of the unchanged 256 MiB upload limit.
    with tempfile.TemporaryFile(dir=spool_dir) as spool:
        async for chunk in store.read(ref):
            size += len(chunk)
            if size > expected.size_bytes or size > MAX_PODCAST_AUDIO_BYTES:
                raise HTTPException(422, detail={"code": "uploaded_size_mismatch"})
            digest.update(chunk)
            await asyncio.to_thread(spool.write, chunk)
        if size != expected.size_bytes or digest.hexdigest() != expected.sha256:
            raise HTTPException(422, detail={"code": "uploaded_sha256_mismatch"})
        after = await store.head(ref)
        if after != metadata:
            raise HTTPException(422, detail={"code": "object_changed_during_verification"})
        duration = await asyncio.to_thread(audio_duration_seconds, spool)
        chapters = await asyncio.to_thread(audio_chapters, spool, expected.mime_type)
        return duration, serialized_chapters(chapters)


async def register_audio(
    database: AsyncSession,
    ticket: UploadTicket,
    ref: ObjectRef,
    duration: int | None,
    chapters: list[dict[str, object]],
    request_id: str,
) -> CompletedUpload:
    await _lock_upload_date(database, ticket.trading_date)
    episode = await database.scalar(
        select(PodcastEpisode)
        .where(PodcastEpisode.trading_date == ticket.trading_date)
        .with_for_update()
    )
    created = episode is None
    if episode is None:
        episode = PodcastEpisode(
            trading_date=ticket.trading_date,
            status="draft",
            version=1,
            created_by_user_id=ticket.actor_id,
        )
        database.add(episode)
        await database.flush()
    current = await database.scalar(
        select(PodcastEpisodeAudioVariant)
        .where(
            PodcastEpisodeAudioVariant.episode_id == episode.id,
            PodcastEpisodeAudioVariant.locale == ticket.file.locale,
            PodcastEpisodeAudioVariant.is_active.is_(True),
        )
        .with_for_update()
    )
    version = current.version if current else None
    if version != ticket.file.expected_current_version:
        raise HTTPException(
            409,
            detail={
                "code": "audio_version_conflict",
                "current_versions": {ticket.file.locale: version},
            },
        )
    before = {"status": episode.status, "version": episode.version, "locale_version": version}
    if current is not None:
        current.is_active = False
        current.replaced_at = datetime.now(UTC)
        old = await database.get(Asset, current.asset_id)
        if old is not None:
            old.status = AssetStatus.ARCHIVED
        await database.flush()
    asset = Asset(
        id=ticket.asset_id,
        bucket=ref.bucket,
        object_key=ref.key,
        kind=AssetKind.AUDIO,
        mime_type=ticket.file.mime_type,
        size_bytes=ticket.file.size_bytes,
        sha256=ticket.file.sha256,
        locale=ticket.file.locale,
        localized_titles={},
        status=AssetStatus.ACTIVE,
        uploaded_by_user_id=ticket.actor_id,
    )
    database.add(asset)
    await database.flush()
    database.add(
        PodcastEpisodeAudioVariant(
            episode_id=episode.id,
            locale=ticket.file.locale,
            version=(version or 0) + 1,
            asset_id=asset.id,
            is_active=True,
            activated_by_user_id=ticket.actor_id,
            duration_seconds=duration,
            chapters=chapters,
            chapters_source="file" if chapters else "none",
        )
    )
    # Do not undo an administrator's intervening unpublish/edit. Other locales
    # may complete independently without a batch-wide version fence.
    if (
        episode.status == "draft"
        and not ticket.began_published
        and (created or episode.version == ticket.episode_version)
    ):
        episode.status = "published"
        episode.published_at = datetime.now(UTC)
        episode.published_by_user_id = ticket.actor_id
    episode.version += 1
    record_audit_event(
        database,
        actor_user_id=ticket.actor_id,
        action="podcast.audio_direct_upload_cutover",
        target_type="podcast_episode",
        target_id=str(episode.id),
        reason=ticket.reason,
        before=before,
        after={
            "status": episode.status,
            "version": episode.version,
            "locale": ticket.file.locale,
            "variant_version": (version or 0) + 1,
            "asset_id": str(asset.id),
            "sha256": asset.sha256,
            "size_bytes": asset.size_bytes,
            "duration_seconds": duration,
        },
        request_id=request_id,
    )
    return CompletedUpload(
        asset_id=asset.id,
        episode_id=episode.id,
        locale=ticket.file.locale,
        sha256=asset.sha256,
        duration_seconds=duration,
    )


@router.post(
    "/api/admin/podcasts/direct-uploads/complete",
    response_model=CompletedUpload,
    operation_id="admin_podcast_direct_upload_complete",
)
async def complete_direct_upload(
    payload: CompleteUploadRequest,
    actor: AssetWrite,
    database: Database,
    store: Store,
    request: Request,
) -> CompletedUpload:
    settings: Settings = request.app.state.settings
    ticket = decode_ticket(payload.upload_token, settings)
    if ticket.actor_id != actor.user.id:
        raise HTTPException(403, detail={"code": "upload_owner_mismatch"})
    if settings.r2_bucket_name is None:
        raise HTTPException(503, detail={"code": "r2_not_configured"})
    ref = ObjectRef(bucket=settings.r2_bucket_name, key=ticket.object_key)
    await lock_upload_object(database, ticket.asset_id)
    previous = await _completed(database, ticket)
    if previous is not None:
        return previous
    if datetime.now(UTC).timestamp() >= ticket.expires:
        raise HTTPException(410, detail={"code": "upload_token_expired"})
    committing = False
    verified = False
    try:
        # Below nginx's ten-minute limit; retain the object for a later retry.
        async with asyncio.timeout(540):
            duration, chapters = await verify_audio(
                store,
                ref,
                ticket,
                spool_dir=settings.podcast_media_spool_dir
                if settings.environment == "production"
                else None,
            )
        verified = True
        result = await register_audio(
            database, ticket, ref, duration, chapters, request.state.request_id
        )
        committing = True
        await database.commit()
        return result
    except HTTPException as error:
        await database.rollback()
        if error.status_code == 422 or (verified and error.status_code == 409):
            await lock_upload_object(database, ticket.asset_id)
            await delete_unreferenced(database, store, ref)
            await database.commit()
        raise
    except SQLAlchemyError:
        await database.rollback()
        # A commit exception can mean the commit reached PostgreSQL. Recheck on
        # a fresh transaction under the same object lock before compensation.
        try:
            await lock_upload_object(database, ticket.asset_id)
            recovered = await _completed(database, ticket)
            if recovered is not None:
                return recovered
            await delete_unreferenced(database, store, ref)
            await database.commit()
        except SQLAlchemyError:
            await database.rollback()
            logger.warning("Podcast database outcome unavailable; object retained for cleanup")
        raise HTTPException(
            503, detail={"code": "upload_registration_unavailable", "commit_attempted": committing}
        ) from None
    except (*OBJECT_STORE_ERRORS, TimeoutError, OSError):
        await database.rollback()
        raise HTTPException(503, detail={"code": "upload_storage_unavailable"}) from None
