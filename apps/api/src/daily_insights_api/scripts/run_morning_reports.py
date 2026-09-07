import asyncio
from datetime import date, datetime

from anyio import Path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings, is_placeholder_value
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.logging import configure_logging
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.data_sources.api import (
    RetryPolicy,
    TwelveDataAdapter,
    TwelveDataTransport,
)
from daily_insights_api.modules.reports.launch_manifest import ACTIVE_LAUNCH_MANIFEST
from daily_insights_api.modules.reports.morning_report import (
    run_morning_report_edition,
    run_scheduled_morning_report_markets,
    unpublished_morning_report_markets,
)
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    SameDayRetry,
    due_edition,
    maintain_disabled_heartbeat,
    parse_args,
    run_scheduler,
    run_with_heartbeat,
)

__all__ = [
    "main",
    "maintain_disabled_heartbeat",
    "run_manual_morning_report_edition",
    "run_scheduled_morning_report_edition",
    "run_with_heartbeat",
]

# A morning-report run that raises is retried within the morning window instead
# of crashing the container into an immediate restart loop. Non-exception
# outcomes are final for the day.
RETRY_POLICY = SameDayRetry()


async def run_manual_morning_report_edition(
    session_factory: async_sessionmaker[AsyncSession],
    adapter: TwelveDataAdapter,
    edition_date: date,
    heartbeat: Path,
) -> str:
    """Run a manual edition without the scheduled publication guard."""
    await run_with_heartbeat(
        lambda target_date: run_morning_report_edition(
            session_factory,
            adapter,
            target_date,
        ),
        edition_date,
        heartbeat,
    )
    return "complete"


async def run_scheduled_morning_report_edition(
    session_factory: async_sessionmaker[AsyncSession],
    adapter: TwelveDataAdapter,
    edition_date: date,
    heartbeat: Path,
) -> str:
    """Run only launch markets without an immutable publication for this date."""
    market_codes = await unpublished_morning_report_markets(session_factory, edition_date)
    skipped = [
        market.market_code
        for market in ACTIVE_LAUNCH_MANIFEST.markets
        if market.market_code not in market_codes
    ]
    if not market_codes:
        if skipped:
            emit_event(
                "scheduler.edition.already_published",
                edition_date=edition_date.isoformat(),
                skipped_market_codes=skipped,
            )
        await heartbeat.touch()
        return "complete"
    try:
        locked_skips = await run_scheduled_morning_report_markets(
            session_factory, adapter, edition_date, market_codes
        )
    finally:
        await heartbeat.touch()
    skipped.extend(locked_skips)
    if skipped:
        emit_event(
            "scheduler.edition.already_published",
            edition_date=edition_date.isoformat(),
            skipped_market_codes=skipped,
        )
    return "complete"


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
            # A scheduled restart must not touch a provider once an immutable
            # publication exists, including partial and unavailable editions.
            # --once is deliberately a manual rerun and bypasses this guard.
            if not args.once:
                return await run_scheduled_morning_report_edition(
                    session_factory, adapter, run_date, heartbeat
                )
            return await run_manual_morning_report_edition(
                session_factory, adapter, run_date, heartbeat
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
