from collections.abc import AsyncIterator

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.modules.assets.object_store import ObjectStore


async def get_database_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def get_object_store(request: Request) -> ObjectStore:
    object_store: ObjectStore | None = request.app.state.object_store
    if object_store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "object store is not configured",
        )
    return object_store
