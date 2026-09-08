from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class DatabaseRuntimeSettings(Protocol):
    database_url: str | None


def create_engine(settings: DatabaseRuntimeSettings) -> AsyncEngine:
    assert settings.database_url is not None
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def database_is_ready(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


@asynccontextmanager
async def engine_lifespan(engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    try:
        yield {"engine": engine, "session_factory": create_session_factory(engine)}
    finally:
        await engine.dispose()
