"""Rescan only synchronous-upload objects; never delete DB-referenced versions."""

import asyncio
import logging
import re
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.assets.api import ListableObjectStore, ObjectStore
from daily_insights_api.modules.podcasts.lifecycle import lock_upload_date
from daily_insights_api.modules.podcasts.media_worker import PodcastMediaWorker
from daily_insights_api.modules.podcasts.storage_paths import FINAL_PREFIX, final_audio_date
from daily_insights_api.modules.podcasts.synchronous_upload import (
    delete_unreferenced,
    lock_upload_object,
)

logger = logging.getLogger(__name__)
OBJECT_PATTERN = re.compile(
    r"^podcasts/direct/([0-9]{10})/([0-9]{4}-[0-9]{2}-[0-9]{2})/(?:zh-hant|zh-hans|en)/"
    r"([0-9a-f-]{36})\.(?:mp3|mp4)$"
)


async def cleanup_orphans(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    listing: ListableObjectStore,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int:
    if settings.r2_bucket_name is None:
        return 0
    now = now or datetime.now(UTC)
    grace = timedelta(seconds=settings.podcast_upload_cleanup_grace_seconds)
    deleted = 0
    async for item in listing.list_objects(settings.r2_bucket_name, FINAL_PREFIX):
        if item.last_modified + grace > now:
            continue
        match = OBJECT_PATTERN.fullmatch(item.ref.key)
        asset_id = None
        if match is not None:
            try:
                expires = datetime.fromtimestamp(int(match[1]), UTC)
                trading_date = date.fromisoformat(match[2])
                asset_id = uuid.UUID(match[3])
                if str(asset_id) != match[3]:
                    continue
            except (ValueError, OverflowError):
                continue
            if expires + grace > now:
                continue
        else:
            final_date = final_audio_date(item.ref.key)
            if final_date is None:
                continue
            trading_date = final_date
        async with sessions() as database:
            # Completion/removal/compensation share this lock. Recheck metadata
            # after waiting so a newly recreated object gets its full grace.
            await lock_upload_date(database, trading_date)
            if asset_id is not None:
                await lock_upload_object(database, asset_id)
            fresh = None
            async for candidate in listing.list_objects(item.ref.bucket, item.ref.key):
                if candidate.ref == item.ref:
                    fresh = candidate
                    break
            if fresh is None or fresh.last_modified + grace > now:
                continue
            if await delete_unreferenced(database, store, item.ref):
                deleted += 1
            await database.commit()
    return deleted


async def cleanup_loop(
    sessions: async_sessionmaker[AsyncSession],
    store: ObjectStore | None,
    settings: Settings,
) -> None:
    if store is None or not isinstance(store, ListableObjectStore):
        return
    while True:
        try:
            await cleanup_orphans(sessions, store, store, settings)
            # Legacy sessions are retained as cleanup tombstones during rollout.
            await PodcastMediaWorker(sessions, store, settings).cleanup_one()
        except Exception as error:
            # Retry on the next sweep; DB failures must never permit deletion.
            logger.warning(
                "Podcast orphan sweep deferred", extra={"error_type": type(error).__name__}
            )
        await asyncio.sleep(300)
