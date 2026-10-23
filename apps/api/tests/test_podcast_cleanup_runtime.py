"""No background runtime may invoke unscoped legacy cleanup."""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_phase3_podcast_integration import FakeObjectStore

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.assets.object_store import ListedObject
from daily_insights_api.modules.podcasts import upload_cleanup
from daily_insights_api.modules.podcasts.media_worker import PodcastMediaWorker


class ListableStore(FakeObjectStore):
    async def list_objects(self, bucket: str, prefix: str) -> AsyncIterator[ListedObject]:
        items: tuple[ListedObject, ...] = ()
        for item in items:
            yield item


async def test_api_runtime_only_runs_scoped_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    scoped = AsyncMock(return_value=0)
    legacy = AsyncMock()
    monkeypatch.setattr(upload_cleanup, "cleanup_orphans", scoped)
    monkeypatch.setattr(PodcastMediaWorker, "cleanup_one", legacy)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await upload_cleanup.cleanup_loop(
            async_sessionmaker[AsyncSession](), ListableStore(), Settings(environment="test")
        )
    scoped.assert_awaited_once()
    legacy.assert_not_awaited()


async def test_legacy_worker_runtime_retains_unscoped_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AsyncMock(return_value=False)
    legacy = AsyncMock()
    monkeypatch.setattr(PodcastMediaWorker, "process_one", process)
    monkeypatch.setattr(PodcastMediaWorker, "cleanup_one", legacy)
    monkeypatch.setattr(PodcastMediaWorker, "_heartbeat", Mock())
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    worker = PodcastMediaWorker(
        async_sessionmaker[AsyncSession](), FakeObjectStore(), Settings(environment="test")
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
    process.assert_awaited_once()
    legacy.assert_not_awaited()
