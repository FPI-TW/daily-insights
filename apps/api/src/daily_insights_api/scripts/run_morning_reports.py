import asyncio
from datetime import date, datetime

from anyio import Path

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings, is_placeholder_value
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.logging import configure_logging
from daily_insights_api.modules.data_sources.api import (
    RetryPolicy,
    TwelveDataAdapter,
    TwelveDataTransport,
)
from daily_insights_api.modules.reports.morning_report import run_morning_report_edition
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    SameDayRetry,
    due_edition,
    maintain_disabled_heartbeat,
    parse_args,
    run_scheduler,
    run_with_heartbeat,
)

__all__ = ["main", "maintain_disabled_heartbeat", "run_with_heartbeat"]

# A morning-report run that raises is retried within the morning window instead
# of crashing the container into an immediate restart loop. Non-exception
# outcomes are final for the day.
RETRY_POLICY = SameDayRetry()


async def main() -> None:
    configure_logging()
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

        async def runner(run_date: date) -> str | None:
            return await run_with_heartbeat(
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
                    retry=RETRY_POLICY,
                )
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
