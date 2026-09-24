import asyncio
import hashlib
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, cast

from botocore.exceptions import (
    ChecksumError,
    ClientError,
    HTTPClientError,
    IncompleteReadError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import AssetKind, AssetStatus
from daily_insights_api.modules.assets.api import Asset, ObjectRef, ObjectStore
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.podcasts.models import PodcastEpisode, PodcastEpisodeAudioVariant
from daily_insights_api.modules.podcasts.service import (
    audio_chapters,
    audio_duration_seconds,
    serialized_chapters,
)
from daily_insights_api.modules.podcasts.upload_models import (
    PodcastUploadBatch,
    PodcastUploadSession,
)

MAX_PODCAST_AUDIO_BYTES = 256 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024
LEASE_DURATION = timedelta(minutes=15)
LEASE_RENEW_INTERVAL_SECONDS = 60
MAX_PROCESSING_ATTEMPTS = 5
RETRY_BASE_SECONDS = 15
RETRY_MAX_SECONDS = 300
CLEANUP_RETRY_DELAY = timedelta(minutes=5)
logger = logging.getLogger(__name__)

OBJECT_STORE_ERRORS = (
    ClientError,
    BotoConnectionError,
    HTTPClientError,
    IncompleteReadError,
    ChecksumError,
)


@dataclass(frozen=True)
class UploadClaim:
    session_id: uuid.UUID
    batch_id: uuid.UUID
    lease_token: uuid.UUID
    bucket: str
    object_key: str
    mime_type: str
    size_bytes: int
    expected_sha256: str
    attempts: int


class PodcastMediaWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        settings: Settings,
        *,
        heartbeat_path: Path = Path("/tmp/podcast-media-worker-heartbeat"),
        spool_directory: Path | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._settings = settings
        self._heartbeat_path = heartbeat_path
        self._spool_directory = spool_directory

    async def _claim(self) -> UploadClaim | None:
        now = datetime.now(UTC)
        async with self._session_factory() as database:
            claimable = or_(
                PodcastUploadSession.status == "queued",
                and_(
                    PodcastUploadSession.status == "processing",
                    PodcastUploadSession.lease_until <= now,
                ),
            )
            row = await database.scalar(
                select(PodcastUploadSession)
                .where(claimable)
                .order_by(PodcastUploadSession.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            token = uuid.uuid4()
            row.status = "processing"
            row.lease_token = token
            row.lease_until = now + LEASE_DURATION
            row.attempts += 1
            row.error_code = None
            await database.commit()
            return UploadClaim(
                session_id=row.id,
                batch_id=row.batch_id,
                lease_token=token,
                bucket=self._settings.r2_bucket_name or "",
                object_key=row.object_key,
                mime_type=row.mime_type,
                size_bytes=row.size_bytes,
                expected_sha256=row.expected_sha256,
                attempts=row.attempts,
            )

    async def _renew(self, claim: UploadClaim) -> bool:
        async with self._session_factory() as database:
            result = await database.execute(
                update(PodcastUploadSession)
                .where(
                    PodcastUploadSession.id == claim.session_id,
                    PodcastUploadSession.status == "processing",
                    PodcastUploadSession.lease_token == claim.lease_token,
                )
                .values(lease_until=datetime.now(UTC) + LEASE_DURATION)
                .returning(PodcastUploadSession.id)
            )
            renewed_session_id = result.scalar_one_or_none()
            await database.commit()
            return renewed_session_id == claim.session_id

    async def _mark_failed(self, claim: UploadClaim, error_code: str) -> None:
        async with self._session_factory() as database:
            await database.execute(
                update(PodcastUploadSession)
                .where(
                    PodcastUploadSession.id == claim.session_id,
                    PodcastUploadSession.status == "processing",
                    PodcastUploadSession.lease_token == claim.lease_token,
                )
                .values(
                    status="failed",
                    error_code=error_code,
                    lease_token=None,
                    lease_until=None,
                )
            )
            await database.commit()

    async def _schedule_object_store_retry(self, claim: UploadClaim, error: Exception) -> None:
        terminal = claim.attempts >= MAX_PROCESSING_ATTEMPTS
        backoff_seconds = min(
            RETRY_BASE_SECONDS * (2 ** max(0, claim.attempts - 1)),
            RETRY_MAX_SECONDS,
        )
        now = datetime.now(UTC)
        values: dict[str, object] = {
            "status": "failed" if terminal else "processing",
            "error_code": (
                "object_store_retry_exhausted" if terminal else "object_store_retry_scheduled"
            ),
            "lease_token": None,
            "lease_until": None if terminal else now + timedelta(seconds=backoff_seconds),
        }
        async with self._session_factory() as database:
            await database.execute(
                update(PodcastUploadSession)
                .where(
                    PodcastUploadSession.id == claim.session_id,
                    PodcastUploadSession.status == "processing",
                    PodcastUploadSession.lease_token == claim.lease_token,
                )
                .values(**values)
            )
            await database.commit()
        logger.warning(
            "Podcast upload object-store attempt failed",
            extra={
                "session_id": str(claim.session_id),
                "attempt": claim.attempts,
                "max_attempts": MAX_PROCESSING_ATTEMPTS,
                "terminal": terminal,
                "error_type": type(error).__name__,
            },
        )

    async def _download_and_verify(
        self, claim: UploadClaim
    ) -> tuple[BinaryIO, str, int | None, list[dict[str, object]]]:
        ref = ObjectRef(bucket=claim.bucket, key=claim.object_key)
        before = await self._store.head(ref)
        if before is None:
            raise UploadValidationError("object_missing")
        if before.size_bytes != claim.size_bytes or before.mime_type.lower() != claim.mime_type:
            raise UploadValidationError("head_metadata_mismatch")
        if before.sha256 is None:
            raise UploadValidationError("object_sha256_missing")
        if before.sha256.lower() != claim.expected_sha256:
            raise UploadValidationError("object_sha256_mismatch")
        spool = tempfile.TemporaryFile(mode="w+b", dir=self._spool_directory)
        digest = hashlib.sha256()
        size_bytes = 0
        renewal_deadline = time.monotonic() + LEASE_RENEW_INTERVAL_SECONDS
        try:
            async for chunk in self._store.read(ref):
                if not chunk:
                    continue
                size_bytes += len(chunk)
                if size_bytes > MAX_PODCAST_AUDIO_BYTES or size_bytes > claim.size_bytes:
                    raise UploadValidationError("uploaded_object_too_large")
                spool.write(chunk)
                digest.update(chunk)
                if time.monotonic() >= renewal_deadline:
                    if not await self._renew(claim):
                        raise UploadLeaseLost
                    renewal_deadline = time.monotonic() + LEASE_RENEW_INTERVAL_SECONDS
            if size_bytes != claim.size_bytes:
                raise UploadValidationError("uploaded_size_mismatch")
            computed_sha256 = digest.hexdigest()
            if computed_sha256 != claim.expected_sha256:
                raise UploadValidationError("object_sha256_mismatch")
            after = await self._store.head(ref)
            if (
                after is None
                or after.size_bytes != claim.size_bytes
                or after.mime_type.lower() != claim.mime_type
                or after.sha256 is None
                or after.sha256.lower() != claim.expected_sha256
            ):
                raise UploadValidationError("object_changed_during_verification")
            if after.sha256.lower() != computed_sha256:
                raise UploadValidationError("object_sha256_mismatch")
            spool.seek(0)
            duration = audio_duration_seconds(spool)
            chapters = serialized_chapters(audio_chapters(spool, claim.mime_type))
            spool.seek(0)
            return spool, computed_sha256, duration, chapters
        except BaseException:
            spool.close()
            raise

    async def _cutover(
        self,
        claim: UploadClaim,
        *,
        sha256: str,
        duration_seconds: int | None,
        chapters: list[dict[str, object]],
    ) -> str:
        now = datetime.now(UTC)
        async with self._session_factory() as database:
            batch = await database.scalar(
                select(PodcastUploadBatch)
                .where(PodcastUploadBatch.id == claim.batch_id)
                .with_for_update()
            )
            if batch is None:
                return "conflict"
            session = await database.scalar(
                select(PodcastUploadSession)
                .where(PodcastUploadSession.id == claim.session_id)
                .with_for_update()
            )
            if (
                session is None
                or session.status != "processing"
                or session.lease_token != claim.lease_token
            ):
                return "conflict"
            if batch.status in {"conflict", "expired"}:
                session.status = "conflict"
                session.error_code = "upload_batch_unavailable"
                session.lease_token = None
                session.lease_until = None
                await database.commit()
                return "conflict"

            if batch.episode_id is None:
                episode = await database.scalar(
                    select(PodcastEpisode)
                    .where(PodcastEpisode.trading_date == batch.trading_date)
                    .with_for_update()
                )
                if episode is None:
                    episode = PodcastEpisode(
                        trading_date=batch.trading_date,
                        status="draft",
                        version=1,
                        created_by_user_id=batch.created_by_user_id,
                    )
                    database.add(episode)
                    try:
                        await database.flush()
                    except IntegrityError:
                        # A competing first batch may have won the trading-date
                        # unique constraint. Its version will be checked below.
                        await database.rollback()
                        return await self._mark_cutover_conflict(claim, "trading_date_conflict")
                batch.episode_id = episode.id
            else:
                episode = await database.scalar(
                    select(PodcastEpisode)
                    .where(PodcastEpisode.id == batch.episode_id)
                    .with_for_update()
                )
            if episode is None:
                batch.status = "conflict"
                session.status = "conflict"
                session.error_code = "episode_missing"
                session.lease_token = None
                session.lease_until = None
                await database.commit()
                return "conflict"
            expected_episode_version = batch.base_episode_version + batch.applied_count
            if episode.version != expected_episode_version:
                batch.status = "conflict"
                session.status = "conflict"
                session.error_code = "episode_version_conflict"
                session.lease_token = None
                session.lease_until = None
                await database.commit()
                return "conflict"

            current = await database.scalar(
                select(PodcastEpisodeAudioVariant)
                .where(
                    PodcastEpisodeAudioVariant.episode_id == episode.id,
                    PodcastEpisodeAudioVariant.locale == session.locale,
                    PodcastEpisodeAudioVariant.is_active.is_(True),
                )
                .with_for_update()
            )
            actual_locale_version = current.version if current is not None else None
            if actual_locale_version != session.expected_current_version:
                batch.status = "conflict"
                session.status = "conflict"
                session.error_code = "locale_version_conflict"
                session.lease_token = None
                session.lease_until = None
                await database.commit()
                return "conflict"

            asset = Asset(
                id=session.asset_id,
                bucket=claim.bucket,
                object_key=claim.object_key,
                kind=AssetKind.AUDIO,
                mime_type=session.mime_type,
                size_bytes=session.size_bytes,
                sha256=sha256,
                locale=session.locale,
                localized_titles={},
                status=AssetStatus.ACTIVE,
                uploaded_by_user_id=batch.created_by_user_id,
            )
            database.add(asset)
            variant_version = 1 if current is None else current.version + 1
            if current is not None:
                current.is_active = False
                current.replaced_at = now
                old_asset = await database.scalar(
                    select(Asset).where(Asset.id == current.asset_id).with_for_update()
                )
                if old_asset is not None:
                    old_asset.status = AssetStatus.ARCHIVED
            variant = PodcastEpisodeAudioVariant(
                episode_id=episode.id,
                locale=session.locale,
                version=variant_version,
                asset_id=session.asset_id,
                is_active=True,
                activated_by_user_id=batch.created_by_user_id,
                duration_seconds=duration_seconds,
                chapters=chapters,
                chapters_source="file" if chapters else "none",
            )
            database.add(variant)
            before_status = episode.status
            if batch.applied_count == 0 and not batch.began_published and episode.status == "draft":
                episode.status = "published"
                episode.published_at = now
                episode.published_by_user_id = batch.created_by_user_id
            episode.version += 1
            batch.applied_count += 1
            session.status = "completed"
            session.sha256 = sha256
            session.duration_seconds = duration_seconds
            session.chapters = chapters
            session.error_code = None
            session.lease_token = None
            session.lease_until = None
            await database.flush()

            remaining = await database.scalar(
                select(PodcastUploadSession.id)
                .where(
                    PodcastUploadSession.batch_id == batch.id,
                    PodcastUploadSession.id != session.id,
                    PodcastUploadSession.status != "completed",
                )
                .limit(1)
            )
            if remaining is None:
                batch.status = "completed"
            record_audit_event(
                database,
                actor_user_id=batch.created_by_user_id,
                action="podcast.audio_direct_upload_cutover",
                target_type="podcast_episode",
                target_id=str(episode.id),
                reason=batch.reason,
                before={
                    "status": before_status,
                    "version": episode.version - 1,
                    "locale_version": actual_locale_version,
                },
                after={
                    "status": episode.status,
                    "version": episode.version,
                    "locale": session.locale,
                    "variant_version": variant.version,
                    "asset_id": str(asset.id),
                    "sha256": sha256,
                    "size_bytes": session.size_bytes,
                    "duration_seconds": duration_seconds,
                },
                request_id=session.request_id,
            )
            await database.commit()
            return "completed"

    async def _mark_cutover_conflict(self, claim: UploadClaim, error_code: str) -> str:
        async with self._session_factory() as database:
            batch = await database.scalar(
                select(PodcastUploadBatch)
                .where(PodcastUploadBatch.id == claim.batch_id)
                .with_for_update()
            )
            session = await database.scalar(
                select(PodcastUploadSession)
                .where(PodcastUploadSession.id == claim.session_id)
                .with_for_update()
            )
            if (
                batch is not None
                and session is not None
                and session.status == "processing"
                and session.lease_token == claim.lease_token
            ):
                batch.status = "conflict"
                session.status = "conflict"
                session.error_code = error_code
                session.lease_token = None
                session.lease_until = None
                await database.commit()
            return "conflict"

    async def process_one(self) -> bool:
        claim = await self._claim()
        if claim is None:
            return False
        try:
            spool, sha256, duration_seconds, chapters = await self._download_and_verify(claim)
        except UploadLeaseLost:
            return True
        except UploadValidationError as error:
            await self._mark_failed(claim, error.code)
            return True
        except OBJECT_STORE_ERRORS as error:
            await self._schedule_object_store_retry(claim, error)
            return True
        try:
            result = await self._cutover(
                claim,
                sha256=sha256,
                duration_seconds=duration_seconds,
                chapters=chapters,
            )
            if result == "completed":
                self._heartbeat()
        finally:
            spool.close()
        return True

    async def cleanup_one(self) -> bool:
        now = datetime.now(UTC)
        token = uuid.uuid4()
        async with self._session_factory() as database:
            session = await database.scalar(
                select(PodcastUploadSession)
                .where(
                    PodcastUploadSession.status.in_(
                        ("pending_upload", "expired", "failed", "conflict")
                    ),
                    PodcastUploadSession.cleanup_after <= now,
                    or_(
                        PodcastUploadSession.cleanup_lease_until.is_(None),
                        PodcastUploadSession.cleanup_lease_until <= now,
                    ),
                )
                .order_by(PodcastUploadSession.cleanup_after)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if session is None:
                return False
            if session.status == "pending_upload":
                session.status = "expired"
            session.cleanup_lease_token = token
            session.cleanup_lease_until = now + timedelta(minutes=5)
            session.lease_token = None
            session.lease_until = None
            await database.commit()
            session_id = session.id
            ref = ObjectRef(bucket=self._settings.r2_bucket_name or "", key=session.object_key)

        try:
            existing_asset = await self._asset_for_key(ref.key)
            object_found = await self._store.head(ref)
            if object_found is not None and existing_asset is None:
                await self._store.delete(ref)
                next_check = datetime.now(UTC) + timedelta(minutes=5)
            else:
                next_check = datetime.now(UTC) + timedelta(hours=1)
        except OBJECT_STORE_ERRORS as error:
            async with self._session_factory() as database:
                await database.execute(
                    update(PodcastUploadSession)
                    .where(
                        PodcastUploadSession.id == session_id,
                        PodcastUploadSession.cleanup_lease_token == token,
                    )
                    .values(
                        cleanup_lease_token=None,
                        cleanup_lease_until=datetime.now(UTC) + CLEANUP_RETRY_DELAY,
                    )
                )
                await database.commit()
            logger.warning(
                "Podcast upload cleanup object-store operation failed",
                extra={
                    "session_id": str(session_id),
                    "retry_delay_seconds": int(CLEANUP_RETRY_DELAY.total_seconds()),
                    "error_type": type(error).__name__,
                },
            )
            return True
        async with self._session_factory() as database:
            row = await database.scalar(
                select(PodcastUploadSession)
                .where(PodcastUploadSession.id == session_id)
                .with_for_update()
            )
            if row is not None and row.cleanup_lease_token == token:
                row.cleanup_lease_token = None
                row.cleanup_lease_until = next_check
                await database.commit()
        self._heartbeat()
        return True

    async def _asset_for_key(self, object_key: str) -> Asset | None:
        async with self._session_factory() as database:
            return cast(
                Asset | None,
                await database.scalar(select(Asset).where(Asset.object_key == object_key)),
            )

    def _heartbeat(self) -> None:
        self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self._heartbeat_path.touch()

    async def run(self) -> None:
        self._heartbeat()
        while True:
            worked = await self.process_one()
            cleaned = await self.cleanup_one()
            self._heartbeat()
            if not worked and not cleaned:
                await asyncio.sleep(self._settings.podcast_media_poll_seconds)


class UploadValidationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UploadLeaseLost(RuntimeError):
    pass
