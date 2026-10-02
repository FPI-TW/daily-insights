"""Rescan only synchronous-upload objects; never delete DB-referenced versions."""

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.assets.api import ListableObjectStore, ObjectStore
from daily_insights_api.modules.podcasts.media_worker import PodcastMediaWorker
from daily_insights_api.modules.podcasts.synchronous_upload import (
    UPLOAD_PREFIX,
    delete_unreferenced,
    lock_upload_object,
)

logger = logging.getLogger(__name__)
OBJECT_PATTERN = re.compile(
    r"^podcasts/direct/(\d{10})/\d{4}-\d{2}-\d{2}/(?:zh-hant|zh-hans|en)/"
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
    async for item in listing.list_objects(settings.r2_bucket_name, UPLOAD_PREFIX):
        match = OBJECT_PATTERN.fullmatch(item.ref.key)
        if match is None:
            continue
        try:
            expires = datetime.fromtimestamp(int(match[1]), UTC)
            asset_id = uuid.UUID(match[2])
        except (ValueError, OverflowError):
            continue
        if expires + grace > now or item.last_modified + grace > now:
            continue
        async with sessions() as database:
            await lock_upload_object(database, asset_id)
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
