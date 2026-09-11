"""Keep index_daily_bars current.

Two sources, because two exchanges publish these series: eight symbols come
from Yahoo Finance in one batched call, and ^TWII comes from TWSE, which splits
one bar across two month-wide reports. Both are refreshed by every invocation.

Two uses, one code path, because the store upserts on (symbol, trade_date):

    --once --period 2y --taiex-months 25   explicit two-year backfill
    (no arguments)                         7d of Yahoo, 2 months of TWSE

When a newly tracked Yahoo symbol has no durable bars, its first normal
short-window refresh automatically fetches two years for that symbol only.
Later scheduled refreshes return to seven days, so a catalog rollout needs no
separate manual empty-database backfill. ^TWII gets the same treatment counted
in months: an empty series widens the default window to two years by itself, so
a provider switch only has to delete the old rows. An explicit --taiex-months is
always taken literally.

The Yahoo window is 7d rather than 1d on purpose. At 08:00 Taipei the US
session that closed a few hours earlier is still "today" in its own exchange
timezone, so the adapter drops it as unsettled and it only lands the following
morning. A week-wide window absorbs that lag, plus public holidays and a missed
run, without any catch-up logic. The TWSE window is two months for the same
reason: a run on the first of a month still repairs the end of the previous one.

TWSE spaces its own requests, so each TAIEX month costs two intervals. A
25-month backfill is minutes of wall clock and belongs here rather than on the
admin endpoint, which has to answer inside the proxy's 60s budget.
"""

import argparse
import asyncio
from datetime import date, datetime

from anyio import Path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings, get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.data_sources.api import (
    DataSourceError,
    TwseAdapter,
    YfinanceAdapter,
)
from daily_insights_api.modules.markets.api import (
    AUTOMATIC_SHORT_REFRESH_PERIOD,
    TAIEX_INCREMENTAL_MONTHS,
    TAIEX_SYMBOL,
    YFINANCE_INDICES,
    refresh_index_daily_bars,
    refresh_taiex_daily_bars,
    select_taiex_refresh_months,
)
from daily_insights_api.modules.reports.scheduler import (
    TAIPEI,
    SameDayRetry,
    maintain_disabled_heartbeat,
    run_scheduler,
    run_with_heartbeat,
)

__all__ = ["main", "parse_args", "run_refresh"]

HEARTBEAT_PATH = "/tmp/index-daily-bars-heartbeat"
SCHEDULED_PERIOD = AUTOMATIC_SHORT_REFRESH_PERIOD
# ^TWII comes from TWSE, which is keyed on months rather than a lookback
# window. Two covers the edition month and repairs the end of the one before,
# which is what the 7d Yahoo window does for the other symbols.
SCHEDULED_TAIEX_MONTHS = TAIEX_INCREMENTAL_MONTHS
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
    parser.add_argument(
        "--taiex-months",
        type=int,
        default=SCHEDULED_TAIEX_MONTHS,
        help=(
            f"months of {TAIEX_SYMBOL} to refresh from TWSE, counting back from today "
            f"(default: {SCHEDULED_TAIEX_MONTHS}; use 25 for a two-year backfill). "
            "Each month costs two spaced requests."
        ),
    )
    return parser.parse_args(args)


async def run_refresh(
    session_factory: async_sessionmaker[AsyncSession],
    period: str,
    *,
    timeout_seconds: float,
    settings: Settings,
    taiex_months_back: int = SCHEDULED_TAIEX_MONTHS,
) -> str:
    adapter = YfinanceAdapter(timeout_seconds=timeout_seconds)
    async with session_factory.begin() as database:
        refreshed, failures = await refresh_index_daily_bars(
            database,
            adapter=adapter,
            symbols=list(YFINANCE_INDICES),
            period=period,
        )
    # A disabled provider is a deliberate configuration, not a failed run. The
    # Yahoo half says the same thing by never entering the schedule at all (see
    # `main`), and no retry can turn a flag on, so the TAIEX half is skipped
    # rather than failed -- otherwise every run would report failure and drag
    # the eight Yahoo symbols through a same-day retry until noon with it.
    taiex_skipped = not settings.twse_enabled
    taiex_stored, taiex_error = (
        (0, None)
        if taiex_skipped
        else await _refresh_taiex(session_factory, settings=settings, months_back=taiex_months_back)
    )
    emit_event(
        "index_daily_bars.refreshed",
        period=period,
        stored=sum(entry.stored_count for entry in refreshed) + taiex_stored,
        succeeded=[entry.result.symbol for entry in refreshed],
        failed=[entry.symbol for entry in failures]
        + ([TAIEX_SYMBOL] if taiex_error is not None else []),
        taiex_months=taiex_months_back,
        taiex_error=taiex_error,
        taiex_skipped=taiex_skipped,
    )
    if taiex_error is not None:
        return "failed"
    # The transaction has committed by this point, and each symbol wrote inside
    # its own savepoint, so the symbols listed in `succeeded` are durable even
    # when others failed. Reporting "failed" schedules a same-day retry, which
    # only rewrites identical rows for the symbols that already worked.
    return "failed" if failures else "complete"


async def _refresh_taiex(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    months_back: int,
) -> tuple[int, str | None]:
    """Refresh ^TWII from TWSE, returning rows stored and any failure message.

    Callers check `twse_enabled` first; reaching here means the provider is on,
    so anything that goes wrong from this point is a real failure worth
    retrying. Kept out of `run_refresh`'s transaction: TWSE spaces its
    requests, so a long backfill would otherwise hold one open for minutes.
    """
    if months_back < 1:
        raise ValueError("taiex months must be at least 1")
    today = datetime.now(TAIPEI).date()
    adapter = TwseAdapter(
        base_url=settings.twse_base_url,
        timeout_seconds=settings.twse_timeout_seconds,
        request_interval_seconds=settings.twse_request_interval_seconds,
        max_attempts=settings.twse_retry_attempts,
    )
    try:
        async with session_factory.begin() as database:
            # Only the default incremental window widens for an empty series;
            # an explicit --taiex-months is taken literally.
            months = await select_taiex_refresh_months(
                database, today=today, requested_months=months_back
            )
            refreshed = await refresh_taiex_daily_bars(database, adapter=adapter, months=months)
    except DataSourceError as error:
        return 0, f"{type(error).__name__}: {error}"
    finally:
        await adapter.close()
    return refreshed.stored_count, None


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
            settings=settings,
            taiex_months_back=args.taiex_months,
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
