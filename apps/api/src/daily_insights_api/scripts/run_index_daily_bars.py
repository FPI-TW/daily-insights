"""Keep index_daily_bars current from Yahoo Finance.

Two uses, one code path, because the store upserts on (symbol, trade_date):

    --once --period 2y   explicit two-year backfill for all tracked symbols
    (no arguments)       the scheduled container, 7d every morning

When a newly tracked symbol has no durable bars, its first normal short-window
refresh automatically fetches two years for that symbol only. Later scheduled
refreshes return to seven days, so a catalog rollout needs no separate manual
empty-database backfill.

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
    maintain_disabled_heartbeat,
    run_scheduler,
    run_with_heartbeat,
)

__all__ = ["main", "parse_args", "run_refresh"]

HEARTBEAT_PATH = "/tmp/index-daily-bars-heartbeat"
SCHEDULED_PERIOD = "7d"
# A failed run retries inside the same morning rather than restarting the
# container, matching the other two schedulers.
RETRY_POLICY = SameDayRetry()


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Deliberately not reports.scheduler.parse_args.

    That one offers --edition-date, which this job cannot honour: the window is
    always `--period` counted back from now, so a date would be accepted and
    silently ignored by an operator who thought they were backfilling a past
    day. Reach further back with --period instead; the store upserts, so
    repeating a window is safe.
    """
    parser = argparse.ArgumentParser(description="Refresh tracked index daily bars")
    parser.add_argument("--once", action="store_true", help="run one refresh and exit")
    parser.add_argument(
        "--period",
        default=SCHEDULED_PERIOD,
        help=f"yfinance history window (default: {SCHEDULED_PERIOD}; use 2y to backfill)",
    )
    return parser.parse_args(args)


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
    # The transaction has committed by this point, and each symbol wrote inside
    # its own savepoint, so the symbols listed in `succeeded` are durable even
    # when others failed. Reporting "failed" schedules a same-day retry, which
    # only rewrites identical rows for the symbols that already worked.
    return "failed" if failures else "complete"


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    heartbeat = Path(HEARTBEAT_PATH)
    await heartbeat.touch()
    if not settings.yfinance_enabled and not args.once:
        await maintain_disabled_heartbeat(heartbeat)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    # The scheduler's date decides whether today's run has happened yet, not
    # what to fetch: the window is always `--period` counted back from now.
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
            await runner(datetime.now(TAIPEI).date())
        else:
            await run_scheduler(runner, now=lambda: datetime.now(TAIPEI), retry=RETRY_POLICY)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
