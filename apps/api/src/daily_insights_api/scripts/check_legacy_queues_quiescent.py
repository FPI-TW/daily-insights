from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from daily_insights_api.core.config import Settings

ACTIVE_LEGACY_QUEUE = 12


async def _active_count(connection: AsyncConnection, table: str) -> int:
    relation = await connection.scalar(
        text("SELECT to_regclass(:table)"), {"table": f"public.{table}"}
    )
    if relation is None:
        return 0
    # Table names are fixed application constants, never external input.
    value = await connection.scalar(
        text(f"SELECT count(*) FROM {table} WHERE status IN ('pending', 'running')")
    )
    return int(value or 0)


async def _main() -> int:
    settings = Settings()
    if settings.database_url is None:
        raise RuntimeError("DAILY_INSIGHTS_DATABASE_URL is required")
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            counts = [
                await _active_count(connection, table)
                for table in (
                    "data_management_runs",
                    "legacy_data_management_runs",
                    "report_pipeline_runs",
                )
            ]
        return ACTIVE_LEGACY_QUEUE if any(counts) else 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
