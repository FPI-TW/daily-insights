"""Keep index_daily_bars current from Yahoo Finance.

Two uses, one code path, because the store upserts on (symbol, trade_date):

    --once --period 2y   one-time backfill of an empty database
    (no arguments)       the scheduled container, 7d every morning

The nightly window is 7d rather than 1d on purpose. At 08:00 Taipei the US
session that closed a few hours earlier is still "today" in its own exchange
timezone, so the adapter drops it as unsettled and it only lands the following
morning. A week-wide window absorbs that lag, plus public holidays and a missed
run, without any catch-up logic.
"""

import argparse
import asyncio
from datetime import date, datetime

from anyio import Path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.data_sources.api import TRACKED_INDICES, YfinanceAdapter
from daily_insights_api.modules.markets.api import refresh_index_daily_bars
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    SameDayRetry,
    due_edition,
    maintain_disabled_heartbeat,
    parse_args,
    run_scheduler,
    run_with_heartbeat,
)

__all__ = ["main", "run_refresh"]

HEARTBEAT_PATH = "/tmp/index-daily-bars-heartbeat"
SCHEDULED_PERIOD = "7d"
# A failed run retries inside the same morning rather than restarting the
# container, matching the other two schedulers.
RETRY_POLICY = SameDayRetry()


def configure_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--period",
        default=SCHEDULED_PERIOD,
        help=f"yfinance history window (default: {SCHEDULED_PERIOD}; use 2y to backfill)",
    )


async def run_refresh(
    session_factory: async_sessionmaker[AsyncSession],
    period: str,
    *,
    timeout_seconds: float,
) -> str:
    adapter = YfinanceAdapter(timeout_seconds=timeout_seconds)
    async with session_factory.begin() as database:
        refreshed, failures = await refresh_index_daily_bars(
            database,
            adapter=adapter,
            symbols=list(TRACKED_INDICES),
            period=period,
        )
    emit_event(
        "index_daily_bars.refreshed",
        period=period,
        stored=sum(entry.stored_count for entry in refreshed),
        succeeded=[entry.result.symbol for entry in refreshed],
        failed=[entry.symbol for entry in failures],
    )
    # Any failed symbol makes the run retryable; the successful symbols are
    # already committed, and re-running them only rewrites identical rows.
    return "failed" if failures else "complete"


async def main() -> None:
    args = parse_args(
        description="Refresh tracked index daily bars",
        configure=configure_arguments,
    )
    settings = get_settings()
    heartbeat = Path(HEARTBEAT_PATH)
    await heartbeat.touch()
    if not settings.yfinance_enabled and not args.once:
        await maintain_disabled_heartbeat(heartbeat)
    now = datetime.now(TAIPEI)
    edition = args.edition_date or (now.date() if args.once else due_edition(now))
    if args.once and edition is None:
        raise SystemExit("no run is due yet; pass --edition-date for a manual run")
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    async def refresh(_: date) -> str:
        return await run_refresh(
            session_factory,
            args.period,
            timeout_seconds=settings.yfinance_timeout_seconds,
        )

    async def runner(run_date: date) -> str | None:
        return await run_with_heartbeat(refresh, run_date, heartbeat)

    try:
        if args.once:
            assert edition is not None
            await runner(edition)
        else:
            await run_scheduler(runner, now=lambda: datetime.now(TAIPEI), retry=RETRY_POLICY)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
