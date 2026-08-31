import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime

from anyio import Path

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings, is_placeholder_value
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.data_sources.api import (
    RetryPolicy,
    TwelveDataAdapter,
    TwelveDataTransport,
)
from daily_insights_api.modules.reports.morning_report import run_morning_report_edition
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    due_edition,
    parse_args,
    run_scheduler,
)

ReportRunner = Callable[[date], Awaitable[None]]
Sleeper = Callable[[float], Awaitable[None]]


async def maintain_disabled_heartbeat(
    heartbeat: Path,
    *,
    sleep: Sleeper = asyncio.sleep,
) -> None:
    while True:
        await heartbeat.touch()
        await sleep(60)


async def run_with_heartbeat(
    runner: ReportRunner,
    edition_date: date,
    heartbeat: Path,
) -> None:
    try:
        await runner(edition_date)
    finally:
        await heartbeat.touch()


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    heartbeat = Path("/tmp/morning-report-heartbeat")
    await heartbeat.touch()
    if not settings.morning_reports_enabled and not args.once:
        await maintain_disabled_heartbeat(heartbeat)
    now = datetime.now(TAIPEI)
    edition = args.edition_date or (now.date() if args.once else due_edition(now))
    if args.once and edition is None:
        raise SystemExit("no edition is due yet; pass --edition-date for a manual run")
    api_key = settings.twelve_data_api_key
    if api_key is None:
        raise SystemExit("Twelve Data API key is required for morning-report generation")
    api_key_value = api_key.get_secret_value()
    if not api_key_value.strip() or is_placeholder_value(api_key_value):
        raise SystemExit("Twelve Data API key is required for morning-report generation")
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    async with TwelveDataTransport(
        base_url=settings.twelve_data_base_url,
        api_key=api_key,
        timeout_seconds=settings.twelve_data_timeout_seconds,
        retry_policy=RetryPolicy(max_attempts=settings.twelve_data_retry_attempts),
        max_concurrency=settings.twelve_data_max_concurrency,
    ) as transport:
        adapter = TwelveDataAdapter(transport)

        async def runner(run_date: date) -> None:
            await run_with_heartbeat(
                lambda target_date: run_morning_report_edition(
                    session_factory,
                    adapter,
                    target_date,
                ),
                run_date,
                heartbeat,
            )

        try:
            if args.once:
                assert edition is not None
                await runner(edition)
            else:
                await run_scheduler(
                    runner,
                    now=lambda: datetime.now(TAIPEI),
                )
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
