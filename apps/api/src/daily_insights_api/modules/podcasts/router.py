import hashlib
import json
import mimetypes
import uuid
from datetime import UTC, date, datetime
from pathlib import PurePath
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.assets.api import (
    BROWSER_PODCAST_AUDIO_EXTENSIONS,
    ObjectStore,
)
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.identity.api import (
    AuthContext,
    require_csrf_roles,
    require_roles,
)
from daily_insights_api.modules.podcasts.api import (
    Locale,
    PodcastAudioImportRequest,
    PodcastAudioPlaybackResponse,
    PodcastEpisodeAdminResponse,
    PodcastEpisodeCreate,
    PodcastEpisodeDetailResponse,
    PodcastEpisodeSummaryResponse,
    PodcastEpisodeUpdate,
    PodcastPublicationRequest,
    PodcastUploadReason,
)
from daily_insights_api.modules.podcasts.models import PodcastEpisode
from daily_insights_api.modules.podcasts.service import (
    PodcastAudioUpload,
    PodcastConflictError,
    PodcastMediaUnavailableError,
    PodcastNotFoundError,
    PodcastPublicationError,
    ensure_publishable,
    episode_admin_response,
    get_episode,
    import_audio,
    list_published_episodes,
    published_episode_detail,
    replace_metadata,
    sign_episode_audio,
    upload_audio_batch,
)
from daily_insights_api.web.dependencies import get_database_session, get_object_store

router = APIRouter(tags=["podcasts"])
AdminRead = Annotated[
    AuthContext,
    Depends(require_roles(SystemRole.ADMIN, SystemRole.ASSET_MANAGER)),
]
AdminWrite = Annotated[AuthContext, Depends(require_csrf_roles(SystemRole.ADMIN))]
AssetWrite = Annotated[
    AuthContext,
    Depends(require_csrf_roles(SystemRole.ADMIN, SystemRole.ASSET_MANAGER)),
]
CustomerRead = Annotated[
    AuthContext,
    Depends(
        require_roles(
            SystemRole.ADMIN,
            SystemRole.ASSET_MANAGER,
            SystemRole.ORG_MEMBER,
        )
    ),
]
Database = Annotated[AsyncSession, Depends(get_database_session)]
Store = Annotated[ObjectStore, Depends(get_object_store)]
MAX_PODCAST_AUDIO_BYTES = 256 * 1024 * 1024
INTERNAL_CUSTOMER_ORGANIZATION: Literal["admin"] = "admin"


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Podcast episode not found")


def _require_customer_organization(
    actor: AuthContext,
) -> uuid.UUID | Literal["admin"]:
    if actor.user.system_role in {SystemRole.ADMIN, SystemRole.ASSET_MANAGER}:
        return INTERNAL_CUSTOMER_ORGANIZATION
    if actor.organization_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "active organization membership required",
        )
    return actor.organization_id


def _require_expected_version(episode: PodcastEpisode, expected_version: int) -> None:
    if episode.version != expected_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "episode_version_conflict",
                "current_version": episode.version,
            },
        )


def _audio_mime_type(upload: UploadFile) -> str:
    extension_types = {
        ".mp3": "audio/mpeg",
        ".mp4": "audio/mp4",
    }
    suffix = PurePath(upload.filename or "").suffix.lower()
    expected = extension_types.get(suffix)
    if expected is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "unsupported_audio_type"},
        )
    declared = (upload.content_type or "").lower()
    aliases = {
        "audio/mp3": "audio/mpeg",
        "video/mp4": "audio/mp4",
    }
    declared = aliases.get(declared, declared)
    if declared in BROWSER_PODCAST_AUDIO_EXTENSIONS and declared == expected:
        return expected
    guessed = mimetypes.guess_type(upload.filename or "")[0] or ""
    guessed = aliases.get(guessed.lower(), guessed.lower())
    if guessed in BROWSER_PODCAST_AUDIO_EXTENSIONS and guessed == expected:
        return expected
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": "unsupported_audio_type"},
    )


async def _prepare_audio_upload(locale: Locale, upload: UploadFile) -> PodcastAudioUpload:
    digest = hashlib.sha256()
    size_bytes = 0
    while chunk := await upload.read(1024 * 1024):
        size_bytes += len(chunk)
        if size_bytes > MAX_PODCAST_AUDIO_BYTES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"code": "podcast_audio_too_large"},
            )
        digest.update(chunk)
    if size_bytes == 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "podcast_audio_empty"},
        )
    await upload.seek(0)
    return PodcastAudioUpload(
        locale=locale,
        content=upload.file,
        size_bytes=size_bytes,
        mime_type=_audio_mime_type(upload),
        sha256=digest.hexdigest(),
    )


def _parse_expected_versions(raw: str) -> dict[str, int]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_expected_versions"},
        ) from error
    if not isinstance(value, dict):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_expected_versions"},
        )
    result: dict[str, int] = {}
    for locale, version in value.items():
        if locale not in {"zh-hant", "zh-hans", "en"} or not isinstance(version, int):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "invalid_expected_versions"},
            )
        result[locale] = version
    return result


@router.get(
    "/api/podcasts",
    response_model=list[PodcastEpisodeSummaryResponse],
    operation_id="podcasts_list",
)
async def customer_list(
    actor: CustomerRead,
    database: Database,
    locale: Annotated[Locale, Query()] = "zh-hant",
) -> list[PodcastEpisodeSummaryResponse]:
    _require_customer_organization(actor)
    return await list_published_episodes(database, locale)


@router.get(
    "/api/podcasts/{episode_id}",
    response_model=PodcastEpisodeDetailResponse,
    operation_id="podcasts_get",
)
async def customer_detail(
    episode_id: uuid.UUID,
    actor: CustomerRead,
    database: Database,
    locale: Annotated[Locale, Query()] = "zh-hant",
) -> PodcastEpisodeDetailResponse:
    _require_customer_organization(actor)
    try:
        return await published_episode_detail(database, episode_id, locale)
    except PodcastNotFoundError as error:
        raise _not_found() from error


@router.post(
    "/api/podcasts/{episode_id}/audio-url",
    response_model=PodcastAudioPlaybackResponse,
    operation_id="podcasts_create_audio_url",
)
async def customer_audio_url(
    episode_id: uuid.UUID,
    actor: CustomerRead,
    database: Database,
    store: Store,
    request: Request,
    locale: Annotated[Locale, Query()] = "zh-hant",
) -> PodcastAudioPlaybackResponse:
    _require_customer_organization(actor)
    settings: Settings = request.app.state.settings
    try:
        response = await sign_episode_audio(database, store, settings, episode_id, locale)
    except PodcastNotFoundError as error:
        raise _not_found() from error
    except (PodcastMediaUnavailableError, LookupError, RuntimeError) as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "podcast_audio_unavailable"},
        ) from error
    return response


@router.get(
    "/api/admin/podcasts",
    response_model=list[PodcastEpisodeAdminResponse],
    operation_id="admin_podcasts_list",
)
async def admin_list(
    _: AdminRead,
    database: Database,
) -> list[PodcastEpisodeAdminResponse]:
    episodes = (
        await database.scalars(select(PodcastEpisode).order_by(PodcastEpisode.trading_date.desc()))
    ).all()
    return [await episode_admin_response(database, episode) for episode in episodes]


@router.post(
    "/api/admin/podcasts",
    response_model=PodcastEpisodeAdminResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="admin_podcasts_create",
)
async def admin_create(
    payload: PodcastEpisodeCreate,
    request: Request,
    actor: AdminWrite,
    database: Database,
) -> PodcastEpisodeAdminResponse:
    episode = PodcastEpisode(
        trading_date=payload.trading_date,
        status="draft",
        version=1,
        created_by_user_id=actor.user.id,
    )
    database.add(episode)
    try:
        await database.flush()
        await replace_metadata(database, episode, payload.metadata)
        await database.flush()
    except IntegrityError as error:
        await database.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "trading_date_already_exists"},
        ) from error
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="podcast.episode_created",
        target_type="podcast_episode",
        target_id=str(episode.id),
        after={"trading_date": episode.trading_date.isoformat(), "version": episode.version},
        reason=payload.reason,
        request_id=request.state.request_id,
    )
    await database.commit()
    return await episode_admin_response(database, episode)


@router.put(
    "/api/admin/podcasts/{episode_id}",
    response_model=PodcastEpisodeAdminResponse,
    operation_id="admin_podcasts_update",
)
async def admin_update(
    episode_id: uuid.UUID,
    payload: PodcastEpisodeUpdate,
    request: Request,
    actor: AdminWrite,
    database: Database,
) -> PodcastEpisodeAdminResponse:
    try:
        episode = await get_episode(database, episode_id, for_update=True)
    except PodcastNotFoundError as error:
        raise _not_found() from error
    _require_expected_version(episode, payload.expected_version)
    if episode.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "unpublish before editing metadata")
    before_version = episode.version
    await replace_metadata(database, episode, payload.metadata)
    episode.version += 1
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="podcast.episode_updated",
        target_type="podcast_episode",
        target_id=str(episode.id),
        before={"version": before_version},
        after={"version": episode.version},
        reason=payload.reason,
        request_id=request.state.request_id,
    )
    await database.commit()
    return await episode_admin_response(database, episode)


@router.post(
    "/api/admin/podcasts/{episode_id}/publish",
    response_model=PodcastEpisodeAdminResponse,
    operation_id="admin_podcasts_publish",
)
async def admin_publish(
    episode_id: uuid.UUID,
    payload: PodcastPublicationRequest,
    request: Request,
    actor: AdminWrite,
    database: Database,
) -> PodcastEpisodeAdminResponse:
    try:
        episode = await get_episode(database, episode_id, for_update=True)
    except PodcastNotFoundError as error:
        raise _not_found() from error
    _require_expected_version(episode, payload.expected_version)
    if episode.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "episode is already published")
    try:
        await ensure_publishable(database, episode)
    except PodcastPublicationError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "episode_not_publishable", "message": str(error)},
        ) from error
    episode.status = "published"
    episode.published_at = datetime.now(UTC)
    episode.published_by_user_id = actor.user.id
    episode.version += 1
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="podcast.episode_published",
        target_type="podcast_episode",
        target_id=str(episode.id),
        after={"version": episode.version},
        request_id=request.state.request_id,
    )
    await database.commit()
    return await episode_admin_response(database, episode)


@router.post(
    "/api/admin/podcasts/{episode_id}/unpublish",
    response_model=PodcastEpisodeAdminResponse,
    operation_id="admin_podcasts_unpublish",
)
async def admin_unpublish(
    episode_id: uuid.UUID,
    payload: PodcastPublicationRequest,
    request: Request,
    actor: AdminWrite,
    database: Database,
) -> PodcastEpisodeAdminResponse:
    try:
        episode = await get_episode(database, episode_id, for_update=True)
    except PodcastNotFoundError as error:
        raise _not_found() from error
    _require_expected_version(episode, payload.expected_version)
    if episode.status != "published":
        raise HTTPException(status.HTTP_409_CONFLICT, "episode is not published")
    episode.status = "draft"
    episode.published_at = None
    episode.published_by_user_id = None
    episode.version += 1
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="podcast.episode_unpublished",
        target_type="podcast_episode",
        target_id=str(episode.id),
        after={"version": episode.version},
        request_id=request.state.request_id,
    )
    await database.commit()
    return await episode_admin_response(database, episode)


@router.post(
    "/api/admin/podcasts/uploads",
    response_model=PodcastEpisodeAdminResponse,
    operation_id="admin_podcasts_upload",
)
async def admin_upload(
    request: Request,
    actor: AssetWrite,
    database: Database,
    store: Store,
    trading_date: Annotated[date, Form()],
    reason: Annotated[PodcastUploadReason, Form()],
    confirm_replacement: Annotated[bool, Form()] = False,
    expected_versions: Annotated[str, Form()] = "{}",
    zh_hant: Annotated[UploadFile | None, File()] = None,
    zh_hans: Annotated[UploadFile | None, File()] = None,
    en: Annotated[UploadFile | None, File()] = None,
) -> PodcastEpisodeAdminResponse:
    candidates: tuple[tuple[Locale, UploadFile | None], ...] = (
        ("zh-hant", zh_hant),
        ("zh-hans", zh_hans),
        ("en", en),
    )
    selected = tuple((locale, upload) for locale, upload in candidates if upload is not None)
    if not selected:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "podcast_audio_required"},
        )
    uploads = tuple([await _prepare_audio_upload(locale, upload) for locale, upload in selected])
    episode = await database.scalar(
        select(PodcastEpisode).where(PodcastEpisode.trading_date == trading_date).with_for_update()
    )
    created = episode is None
    if episode is None:
        episode = PodcastEpisode(
            trading_date=trading_date,
            status="draft",
            version=1,
            created_by_user_id=actor.user.id,
        )
        database.add(episode)
        try:
            await database.flush()
        except IntegrityError as error:
            await database.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"code": "trading_date_already_exists"},
            ) from error
    try:
        variants = await upload_audio_batch(
            database,
            store,
            request.app.state.settings,
            episode,
            uploads,
            actor_user_id=actor.user.id,
            confirm_replacement=confirm_replacement,
            expected_versions=_parse_expected_versions(expected_versions),
        )
    except PodcastConflictError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": error.code,
                "current_versions": error.current_versions or {},
            },
        ) from error
    except PodcastMediaUnavailableError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "audio_upload_failed", "message": str(error)},
        ) from error
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="podcast.episode_uploaded" if created else "podcast.audio_uploaded",
        target_type="podcast_episode",
        target_id=str(episode.id),
        after={
            "trading_date": episode.trading_date.isoformat(),
            "locales": [variant.locale for variant in variants],
            "version": episode.version,
        },
        reason=reason,
        request_id=request.state.request_id,
    )
    await database.commit()
    return await episode_admin_response(database, episode)


@router.post(
    "/api/admin/podcasts/{episode_id}/audio-imports",
    response_model=PodcastEpisodeAdminResponse,
    operation_id="admin_podcasts_import_audio",
)
async def admin_import_audio(
    episode_id: uuid.UUID,
    payload: PodcastAudioImportRequest,
    request: Request,
    actor: AssetWrite,
    database: Database,
    store: Store,
) -> PodcastEpisodeAdminResponse:
    try:
        episode = await get_episode(database, episode_id, for_update=True)
        variant = await import_audio(
            database,
            store,
            request.app.state.settings,
            episode,
            payload,
            actor_user_id=actor.user.id,
        )
    except PodcastNotFoundError as error:
        raise _not_found() from error
    except PodcastConflictError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": error.code, "current_version": error.current_version},
        ) from error
    except PodcastMediaUnavailableError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "audio_import_failed", "message": str(error)},
        ) from error
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="podcast.audio_imported",
        target_type="podcast_episode",
        target_id=str(episode.id),
        after={
            "asset_id": str(variant.asset_id),
            "locale": variant.locale,
            "version": variant.version,
        },
        reason=payload.reason,
        request_id=request.state.request_id,
    )
    await database.commit()
    return await episode_admin_response(database, episode)
