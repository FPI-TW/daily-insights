"""Run the independent Taipei 08:00 analyst-viewpoint synchroniser."""

import asyncio
from datetime import date, datetime

from anyio import Path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings, get_settings, is_placeholder_value
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.analyst_viewpoints.service import (
    AnalystViewpointClient,
    AnalystViewpointSyncError,
    record_sync_execution,
    sync_viewpoints,
)
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    EditionRunner,
    SameDayRetry,
    due_edition,
    maintain_disabled_heartbeat,
    parse_args,
    run_scheduler,
    run_with_heartbeat,
)

HEARTBEAT_PATH = "/tmp/analyst-viewpoints-heartbeat"
RETRY_POLICY = SameDayRetry(retry_outcomes=frozenset({"partial"}))


def build_runner(
    session_factory: async_sessionmaker[AsyncSession],
    client: AnalystViewpointClient,
    heartbeat: Path,
) -> EditionRunner:
    async def run(target_date: date) -> str:
        try:
            async with session_factory.begin() as database:
                result = await sync_viewpoints(database, client, target_date)
                await record_sync_execution(
                    database,
                    viewpoint_date=target_date,
                    trigger="scheduler",
                    result=result,
                )
            return result.status
        except Exception as error:
            error_code = (
                error.code
                if isinstance(error, AnalystViewpointSyncError)
                else type(error).__name__[:100]
            )
            # The first transaction has rolled back at this point. Use a new
            # session so a failed provider call or database write cannot hide
            # the scheduler outcome from the admin status page.
            try:
                async with session_factory.begin() as database:
                    await record_sync_execution(
                        database,
                        viewpoint_date=target_date,
                        trigger="scheduler",
                        error_code=error_code,
                    )
            except Exception:
                # The shared scheduler must see the original failure and keep
                # its normal retry policy even if observability storage is down.
                pass
            raise

    async def runner(edition_date: date) -> str | None:
        return await run_with_heartbeat(run, edition_date, heartbeat)

    return runner


async def main() -> None:
    args = parse_args(description="Run the analyst viewpoint scheduler")
    settings: Settings = get_settings()
    heartbeat = Path(HEARTBEAT_PATH)
    await heartbeat.touch()
    if not settings.analyst_viewpoints_enabled and not args.once:
        await maintain_disabled_heartbeat(heartbeat)
    api_key = settings.analyst_viewpoints_api_key
    if (
        api_key is None
        or not api_key.get_secret_value().strip()
        or is_placeholder_value(api_key.get_secret_value())
    ):
        raise SystemExit("analyst viewpoints API key is required")
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    client = AnalystViewpointClient(
        base_url=settings.analyst_viewpoints_base_url,
        api_key=api_key,
        timeout_seconds=settings.analyst_viewpoints_timeout_seconds,
    )
    runner = build_runner(session_factory, client, heartbeat)
    try:
        now = datetime.now(TAIPEI)
        edition = args.edition_date or (now.date() if args.once else due_edition(now))
        if args.once:
            if edition is None:
                raise SystemExit("no edition is due yet; pass --edition-date")
            await runner(edition)
        else:
            await run_scheduler(runner, now=lambda: datetime.now(TAIPEI), retry=RETRY_POLICY)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
