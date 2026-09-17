from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from daily_insights_api.core.config import Settings

SCHEMA_NOT_INSTALLED = 10
ACTIVATION_PENDING = 11


async def _main() -> int:
    settings = Settings()
    if settings.database_url is None:
        raise RuntimeError("DAILY_INSIGHTS_DATABASE_URL is required")
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            relation = await connection.scalar(text("SELECT to_regclass('public.routine_runs')"))
            if relation is None:
                return SCHEMA_NOT_INSTALLED
            routine_count = await connection.scalar(text("SELECT count(*) FROM routine_runs"))
        return ACTIVATION_PENDING if routine_count == 0 else 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
