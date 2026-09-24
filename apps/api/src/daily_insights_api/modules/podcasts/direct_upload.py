import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.assets.api import ObjectRef, ObjectStore
from daily_insights_api.modules.identity.api import AuthContext, require_csrf_roles, require_roles
from daily_insights_api.modules.podcasts.models import (
    PodcastEpisode,
    PodcastEpisodeAudioVariant,
)
from daily_insights_api.modules.podcasts.upload_models import (
    PodcastUploadBatch,
    PodcastUploadSession,
)
from daily_insights_api.web.dependencies import get_database_session, get_object_store

router = APIRouter(tags=["podcast uploads"])
MAX_PODCAST_AUDIO_BYTES = 256 * 1024 * 1024
MIME_BY_EXTENSION = {".mp3": "audio/mpeg", ".mp4": "audio/mp4"}
MIME_ALIASES = {"audio/mp3": "audio/mpeg", "video/mp4": "audio/mp4"}
Locale = Literal["zh-hant", "zh-hans", "en"]
UploadReason = Literal["initial_upload", "update_file", "other"]
PODCAST_UPLOAD_LOCK_NAMESPACE = 0x504F4443
AssetWrite = Annotated[
    AuthContext,
    Depends(require_csrf_roles(SystemRole.ADMIN, SystemRole.ASSET_MANAGER)),
]
AdminRead = Annotated[
    AuthContext,
    Depends(require_roles(SystemRole.ADMIN, SystemRole.ASSET_MANAGER)),
]
Database = Annotated[AsyncSession, Depends(get_database_session)]
Store = Annotated[ObjectStore, Depends(get_object_store)]


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    locale: Locale
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=MAX_PODCAST_AUDIO_BYTES)
    mime_type: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_replacement: bool = False
    expected_current_version: int | None = Field(default=None, gt=0)


class UploadBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    trading_date: date
    reason: UploadReason
    files: tuple[UploadRequest, ...] = Field(min_length=1, max_length=3)

    def validated_files(self) -> tuple[tuple[UploadRequest, str, str], ...]:
        locales = [item.locale for item in self.files]
        if len(locales) != len(set(locales)):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "duplicate_upload_locale"},
            )
        result: list[tuple[UploadRequest, str, str]] = []
        for item in self.files:
            extension = PurePosixPath(item.filename).suffix.lower()
            expected_mime = MIME_BY_EXTENSION.get(extension)
            actual_mime = MIME_ALIASES.get(item.mime_type.lower(), item.mime_type.lower())
            if expected_mime is None or actual_mime != expected_mime:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"code": "unsupported_audio_type", "locale": item.locale},
                )
            if item.expected_current_version is not None and not item.confirm_replacement:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"code": "replacement_confirmation_required", "locale": item.locale},
                )
            result.append((item, extension[1:], expected_mime))
        return tuple(sorted(result, key=lambda item: item[0].locale))


class FileInitResponse(BaseModel):
    session_id: uuid.UUID
    asset_id: uuid.UUID
    locale: Locale
    object_key: str
    upload_url: str
    required_headers: dict[str, str]
    expires_at: datetime
    status: str
    request_id: str


class UploadBatchInitResponse(BaseModel):
    batch_id: uuid.UUID
    trading_date: date
    reason: UploadReason
    base_episode_version: int | None
    status: str
    expires_at: datetime
    files: list[FileInitResponse]


class UploadFileStatus(BaseModel):
    session_id: uuid.UUID
    asset_id: uuid.UUID
    locale: Locale
    status: str
    error_code: str | None
    sha256: str | None
    duration_seconds: int | None


class UploadFinalizeResponse(BaseModel):
    batch_id: uuid.UUID
    session_id: uuid.UUID
    locale: Locale
    status: str
    error_code: str | None


class UploadBatchStatusResponse(BaseModel):
    batch_id: uuid.UUID
    status: str
    applied_count: int
    files: list[UploadFileStatus]


def _batch_status(sessions: list[PodcastUploadSession], batch: PodcastUploadBatch) -> str:
    if batch.status == "expired":
        return "expired"
    if batch.status == "conflict" or any(session.status == "conflict" for session in sessions):
        return "conflict"
    if sessions and all(session.status == "completed" for session in sessions):
        return "completed"
    terminal = {"completed", "failed", "conflict", "expired"}
    if sessions and all(session.status == "expired" for session in sessions):
        return "expired"
    if sessions and all(session.status in terminal for session in sessions):
        if any(session.status == "completed" for session in sessions):
            return "partial"
        return "failed"
    if any(session.status == "failed" for session in sessions) and any(
        session.status == "completed" for session in sessions
    ):
        return "partial"
    if batch.applied_count > 0:
        return "partial"
    return "pending"


def _init_response(
    batch: PodcastUploadBatch, sessions: list[PodcastUploadSession]
) -> UploadBatchInitResponse:
    by_locale = sorted(sessions, key=lambda session: session.locale)
    return UploadBatchInitResponse(
        batch_id=batch.id,
        trading_date=batch.trading_date,
        reason=batch.reason,
        base_episode_version=None if batch.episode_id is None else batch.base_episode_version,
        status=_batch_status(sessions, batch),
        expires_at=batch.expires_at,
        files=[
            FileInitResponse(
                session_id=session.id,
                asset_id=session.asset_id,
                locale=session.locale,
                object_key=session.object_key,
                upload_url=session.upload_url,
                required_headers={
                    "Content-Type": session.mime_type,
                    "If-None-Match": "*",
                    "x-amz-meta-sha256": session.expected_sha256,
                },
                expires_at=session.expires_at,
                status=session.status,
                request_id=session.request_id,
            )
            for session in by_locale
        ],
    )


def _payload_hash(payload: UploadBatchRequest) -> str:
    canonical = payload.model_dump(mode="json", exclude={"idempotency_key"})
    canonical["files"] = sorted(canonical["files"], key=lambda item: item["locale"])
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


async def _owned_batch(
    database: AsyncSession,
    batch_id: uuid.UUID,
    actor: AuthContext,
    *,
    for_update: bool = False,
) -> PodcastUploadBatch | None:
    statement = select(PodcastUploadBatch).where(
        PodcastUploadBatch.id == batch_id,
        PodcastUploadBatch.created_by_user_id == actor.user.id,
    )
    if for_update:
        statement = statement.with_for_update()
    return cast(PodcastUploadBatch | None, await database.scalar(statement))


async def _sessions_for_batch(
    database: AsyncSession, batch_id: uuid.UUID, *, for_update: bool = False
) -> list[PodcastUploadSession]:
    statement = select(PodcastUploadSession).where(PodcastUploadSession.batch_id == batch_id)
    if for_update:
        statement = statement.with_for_update()
    return list((await database.scalars(statement.order_by(PodcastUploadSession.locale))).all())


async def _lock_upload_date(
    database: AsyncSession,
    trading_date: date,
) -> None:
    """Serialize direct-upload initialization for one logical episode date."""
    await database.execute(
        select(
            func.pg_advisory_xact_lock(
                PODCAST_UPLOAD_LOCK_NAMESPACE,
                trading_date.toordinal(),
            )
        )
    )


async def _expire_stale_pending_sessions(
    database: AsyncSession,
    trading_date: date,
    now: datetime,
) -> None:
    stale_batch_ids = (
        await database.scalars(
            select(PodcastUploadBatch.id)
            .distinct()
            .join(PodcastUploadSession, PodcastUploadSession.batch_id == PodcastUploadBatch.id)
            .where(
                PodcastUploadBatch.trading_date == trading_date,
                PodcastUploadSession.status == "pending_upload",
                PodcastUploadSession.expires_at <= now,
            )
            .order_by(PodcastUploadBatch.id)
        )
    ).all()
    for batch_id in stale_batch_ids:
        batch = await database.scalar(
            select(PodcastUploadBatch).where(PodcastUploadBatch.id == batch_id).with_for_update()
        )
        if batch is None:
            continue
        sessions = (
            await database.scalars(
                select(PodcastUploadSession)
                .where(
                    PodcastUploadSession.batch_id == batch.id,
                    PodcastUploadSession.status == "pending_upload",
                    PodcastUploadSession.expires_at <= now,
                )
                .order_by(PodcastUploadSession.id)
                .with_for_update()
            )
        ).all()
        for session in sessions:
            session.status = "expired"
            session.error_code = "upload_batch_expired"
            session.lease_token = None
            session.lease_until = None
        if sessions:
            remaining = await database.scalar(
                select(PodcastUploadSession.id)
                .where(
                    PodcastUploadSession.batch_id == batch.id,
                    PodcastUploadSession.status != "expired",
                )
                .limit(1)
            )
            if remaining is None:
                batch.status = "expired"


async def _idempotent_init_response(
    database: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    idempotency_key: str,
    payload_sha256: str,
) -> UploadBatchInitResponse | None:
    existing = await database.scalar(
        select(PodcastUploadBatch).where(
            PodcastUploadBatch.created_by_user_id == actor_user_id,
            PodcastUploadBatch.idempotency_key == idempotency_key,
        )
    )
    if existing is None:
        return None
    sessions = await _sessions_for_batch(database, existing.id)
    if existing.payload_sha256 != payload_sha256:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail={"code": "idempotency_key_reused"}
        ) from None
    if existing.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_409_CONFLICT, detail={"code": "upload_batch_expired"})
    return _init_response(existing, sessions)


@router.post(
    "/api/admin/podcasts/upload-batches",
    response_model=UploadBatchInitResponse,
    operation_id="admin_podcast_upload_batch_init",
)
async def init_upload_batch(
    payload: UploadBatchRequest,
    request: Request,
    actor: AssetWrite,
    database: Database,
    store: Store,
) -> UploadBatchInitResponse:
    files = payload.validated_files()
    settings: Settings = request.app.state.settings
    if settings.r2_bucket_name is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "r2_not_configured"}
        )

    digest = _payload_hash(payload)
    replay = await _idempotent_init_response(
        database,
        actor_user_id=actor.user.id,
        idempotency_key=payload.idempotency_key,
        payload_sha256=digest,
    )
    if replay is not None:
        return replay

    await _lock_upload_date(database, payload.trading_date)

    # A concurrent same-key request can commit while this request waits for the
    # date lock. Re-read after acquiring it so retries return that batch.
    replay = await _idempotent_init_response(
        database,
        actor_user_id=actor.user.id,
        idempotency_key=payload.idempotency_key,
        payload_sha256=digest,
    )
    if replay is not None:
        return replay

    now = datetime.now(UTC)
    await _expire_stale_pending_sessions(database, payload.trading_date, now)

    active_locales = sorted(
        set(
            (
                await database.scalars(
                    select(PodcastUploadSession.locale)
                    .join(
                        PodcastUploadBatch,
                        PodcastUploadBatch.id == PodcastUploadSession.batch_id,
                    )
                    .where(
                        PodcastUploadBatch.trading_date == payload.trading_date,
                        PodcastUploadSession.status.in_(("pending_upload", "queued", "processing")),
                    )
                )
            ).all()
        )
    )
    if active_locales:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "upload_in_progress",
                "locales": active_locales,
            },
        )

    episode = await database.scalar(
        select(PodcastEpisode).where(PodcastEpisode.trading_date == payload.trading_date)
    )
    current_versions: dict[str, int | None] = {item.locale: None for item, _, _ in files}
    if episode is not None:
        active = (
            await database.scalars(
                select(PodcastEpisodeAudioVariant).where(
                    PodcastEpisodeAudioVariant.episode_id == episode.id,
                    PodcastEpisodeAudioVariant.locale.in_(list(current_versions)),
                    PodcastEpisodeAudioVariant.is_active.is_(True),
                )
            )
        ).all()
        current_versions.update({variant.locale: variant.version for variant in active})
    replacements: dict[str, int] = {}
    mismatches: dict[str, int | None] = {}
    for item, _, _ in files:
        current_version = current_versions[item.locale]
        if current_version is not None:
            if not item.confirm_replacement:
                replacements[item.locale] = current_version
            elif item.expected_current_version != current_version:
                mismatches[item.locale] = current_version
        elif item.expected_current_version is not None or item.confirm_replacement:
            mismatches[item.locale] = None
    if replacements:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "replacement_confirmation_required",
                "current_versions": replacements,
            },
        )
    if mismatches:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "audio_version_conflict", "current_versions": mismatches},
        )

    expires_at = now + timedelta(seconds=settings.podcast_upload_presign_ttl_seconds)
    grace = timedelta(seconds=settings.podcast_upload_cleanup_grace_seconds)
    batch = PodcastUploadBatch(
        created_by_user_id=actor.user.id,
        idempotency_key=payload.idempotency_key,
        payload_sha256=digest,
        trading_date=payload.trading_date,
        reason=payload.reason,
        episode_id=episode.id if episode is not None else None,
        base_episode_version=episode.version if episode is not None else 1,
        began_published=episode.status == "published" if episode is not None else False,
        applied_count=0,
        status="open",
        request_id=request.state.request_id,
        expires_at=expires_at,
    )
    database.add(batch)
    try:
        await database.flush()
        upload_sessions: list[PodcastUploadSession] = []
        for item, extension, mime_type in files:
            asset_id = uuid.uuid4()
            session_id = uuid.uuid4()
            object_key = (
                f"podcasts/{payload.trading_date.isoformat()}/audio/"
                f"{item.locale}/{asset_id}.{extension}"
            )
            signed_url = await store.presign_put(
                ObjectRef(bucket=settings.r2_bucket_name, key=object_key),
                mime_type=mime_type,
                sha256=item.sha256,
                expires_in=timedelta(seconds=settings.podcast_upload_presign_ttl_seconds),
            )
            session = PodcastUploadSession(
                id=session_id,
                batch_id=batch.id,
                asset_id=asset_id,
                locale=item.locale,
                filename=item.filename,
                object_key=object_key,
                mime_type=mime_type,
                size_bytes=item.size_bytes,
                expected_sha256=item.sha256,
                expected_current_version=current_versions[item.locale],
                upload_url=signed_url,
                request_id=request.state.request_id,
                status="pending_upload",
                expires_at=expires_at,
                cleanup_after=expires_at + grace,
            )
            database.add(session)
            upload_sessions.append(session)
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        existing = await database.scalar(
            select(PodcastUploadBatch).where(
                PodcastUploadBatch.created_by_user_id == actor.user.id,
                PodcastUploadBatch.idempotency_key == payload.idempotency_key,
            )
        )
        if existing is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "trading_date_or_upload_conflict"},
            ) from error
        sessions = await _sessions_for_batch(database, existing.id)
        if existing.payload_sha256 != digest:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail={"code": "idempotency_key_reused"}
            ) from None
        return _init_response(existing, sessions)
    return _init_response(batch, upload_sessions)


@router.post(
    "/api/admin/podcasts/upload-batches/{batch_id}/files/{locale}/finalize",
    response_model=UploadFinalizeResponse,
    operation_id="admin_podcast_upload_finalize",
)
async def finalize_upload(
    batch_id: uuid.UUID,
    locale: Locale,
    actor: AssetWrite,
    database: Database,
    store: Store,
    request: Request,
) -> UploadFinalizeResponse:
    batch = await _owned_batch(database, batch_id, actor, for_update=True)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "upload_batch_not_found"})
    session = await database.scalar(
        select(PodcastUploadSession)
        .where(PodcastUploadSession.batch_id == batch.id, PodcastUploadSession.locale == locale)
        .with_for_update()
    )
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "upload_session_not_found"})
    if session.status != "pending_upload":
        return UploadFinalizeResponse(
            batch_id=batch.id,
            session_id=session.id,
            locale=locale,
            status=session.status,
            error_code=session.error_code,
        )
    if datetime.now(UTC) >= session.cleanup_after:
        session.status = "expired"
        batch.status = "expired"
        await database.commit()
        return UploadFinalizeResponse(
            batch_id=batch.id,
            session_id=session.id,
            locale=locale,
            status=session.status,
            error_code="upload_batch_expired",
        )
    metadata = await store.head(
        ObjectRef(bucket=request.app.state.settings.r2_bucket_name, key=session.object_key)
    )
    if metadata is None:
        await database.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "object_not_uploaded", "session_id": str(session.id)},
        )
    if metadata.size_bytes != session.size_bytes:
        session.status = "failed"
        session.error_code = "uploaded_size_mismatch"
    elif metadata.mime_type.lower() != session.mime_type:
        session.status = "failed"
        session.error_code = "uploaded_mime_type_mismatch"
    elif metadata.sha256 is None:
        session.status = "failed"
        session.error_code = "uploaded_sha256_missing"
    elif metadata.sha256.lower() != session.expected_sha256:
        session.status = "failed"
        session.error_code = "uploaded_sha256_mismatch"
    else:
        session.status = "queued"
        session.error_code = None
    session.request_id = request.state.request_id
    await database.commit()
    return UploadFinalizeResponse(
        batch_id=batch.id,
        session_id=session.id,
        locale=locale,
        status=session.status,
        error_code=session.error_code,
    )


@router.get(
    "/api/admin/podcasts/upload-batches/{batch_id}",
    response_model=UploadBatchStatusResponse,
    operation_id="admin_podcast_upload_batch_status",
)
async def upload_batch_status(
    batch_id: uuid.UUID,
    actor: AdminRead,
    database: Database,
) -> UploadBatchStatusResponse:
    batch = await _owned_batch(database, batch_id, actor)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "upload_batch_not_found"})
    sessions = await _sessions_for_batch(database, batch.id)
    return UploadBatchStatusResponse(
        batch_id=batch.id,
        status=_batch_status(sessions, batch),
        applied_count=batch.applied_count,
        files=[
            UploadFileStatus(
                session_id=session.id,
                asset_id=session.asset_id,
                locale=session.locale,
                status=session.status,
                error_code=session.error_code,
                sha256=session.sha256,
                duration_seconds=session.duration_seconds,
            )
            for session in sessions
        ],
    )
