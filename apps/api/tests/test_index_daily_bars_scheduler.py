from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from daily_insights_api.core.config import Settings
from daily_insights_api.scripts import run_index_daily_bars
from daily_insights_api.scripts.run_index_daily_bars import (
    SCHEDULED_PERIOD,
    SCHEDULED_TAIEX_MONTHS,
    parse_args,
)


def test_the_scheduled_run_defaults_to_a_week_wide_window() -> None:
    args = parse_args([])

    assert args.period == SCHEDULED_PERIOD
    assert args.once is False


def test_a_backfill_reaches_further_back_with_period() -> None:
    args = parse_args(["--once", "--period", "2y"])

    assert args.once is True
    assert args.period == "2y"


def test_edition_date_is_refused_rather_than_silently_ignored() -> None:
    # The sibling schedulers take --edition-date, but this job's window is
    # always `--period` counted back from now. Accepting a date would let an
    # operator believe they were backfilling one past day while today's window
    # ran instead, so argparse must reject it outright.
    with pytest.raises(SystemExit):
        parse_args(["--once", "--edition-date", "2026-09-01"])


def test_the_scheduled_run_defaults_to_two_taiex_months() -> None:
    args = parse_args([])

    assert args.taiex_months == SCHEDULED_TAIEX_MONTHS


def test_a_taiex_backfill_reaches_further_back_with_taiex_months() -> None:
    args = parse_args(["--once", "--taiex-months", "25"])

    assert args.taiex_months == 25


class _NullSessionFactory:
    """`run_refresh` only needs a transaction scope; the work inside is stubbed."""

    def begin(self) -> AbstractAsyncContextManager[object]:
        return _null_transaction()


@asynccontextmanager
async def _null_transaction() -> AsyncIterator[object]:
    yield object()


async def _run_refresh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    twse_enabled: bool,
    taiex_rows: int | None,
) -> tuple[str, dict[str, object]]:
    """Drive `run_refresh` with both providers stubbed, returning its event.

    `taiex_rows=None` asserts the TAIEX provider is never reached.
    """
    events: list[dict[str, object]] = []

    async def yahoo(*_: object, **__: object) -> tuple[list[object], list[object]]:
        return [SimpleNamespace(result=SimpleNamespace(symbol="^DJI"), stored_count=7)], []

    async def taiex(*_: object, **__: object) -> tuple[int, str | None]:
        if taiex_rows is None:
            raise AssertionError("a disabled provider must not be reached")
        return taiex_rows, None

    monkeypatch.setattr(run_index_daily_bars, "YfinanceAdapter", lambda **_: object())
    monkeypatch.setattr(run_index_daily_bars, "refresh_index_daily_bars", yahoo)
    monkeypatch.setattr(run_index_daily_bars, "_refresh_taiex", taiex)
    monkeypatch.setattr(
        run_index_daily_bars, "emit_event", lambda _name, **fields: events.append(fields)
    )

    outcome = await run_index_daily_bars.run_refresh(
        cast(Any, _NullSessionFactory()),
        SCHEDULED_PERIOD,
        timeout_seconds=1.0,
        settings=Settings(environment="test", twse_enabled=twse_enabled),
    )
    return outcome, events[-1]


@pytest.mark.asyncio
async def test_a_disabled_twse_is_skipped_rather_than_failing_the_whole_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flag being off is a configuration, not a failure.

    The Yahoo half says the same thing by never entering the schedule at all.
    Reporting "failed" here would hand SameDayRetry an outcome it retries every
    30 minutes until noon, re-fetching all eight Yahoo symbols each time -- and
    no retry can turn a flag on.
    """
    outcome, event = await _run_refresh(monkeypatch, twse_enabled=False, taiex_rows=None)

    assert outcome == "complete"
    assert event["taiex_skipped"] is True
    assert event["taiex_error"] is None
    assert event["failed"] == []


@pytest.mark.asyncio
async def test_an_enabled_twse_counts_its_rows_and_is_not_marked_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, event = await _run_refresh(monkeypatch, twse_enabled=True, taiex_rows=21)

    assert outcome == "complete"
    assert event["taiex_skipped"] is False
    # Seven Yahoo rows plus twenty-one TAIEX rows.
    assert event["stored"] == 28
