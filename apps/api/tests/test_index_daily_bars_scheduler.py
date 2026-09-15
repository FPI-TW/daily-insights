from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from daily_insights_api.modules.markets.api import TAIEX_SYMBOL
from daily_insights_api.scripts import run_index_daily_bars
from daily_insights_api.scripts.run_index_daily_bars import SCHEDULED_PERIOD, parse_args


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


def test_taiex_months_is_refused_because_this_job_no_longer_fetches_it() -> None:
    # ^TWII is refreshed by the TWSE run, which holds the one client whose
    # request interval keeps the exchange from being asked twice as often as
    # either caller believes. An operator reaching for this flag here has the
    # wrong container.
    with pytest.raises(SystemExit):
        parse_args(["--once", "--taiex-months", "25"])


class _NullSessionFactory:
    """`run_refresh` only needs a transaction scope; the work inside is stubbed."""

    def begin(self) -> AbstractAsyncContextManager[object]:
        return _null_transaction()


@asynccontextmanager
async def _null_transaction() -> AsyncIterator[object]:
    yield object()


async def _run_refresh(monkeypatch: pytest.MonkeyPatch) -> tuple[str, dict[str, object], list[str]]:
    """Drive `run_refresh` with Yahoo stubbed, returning its event and symbols."""
    events: list[dict[str, object]] = []
    asked: list[str] = []

    async def yahoo(*_: object, **kwargs: object) -> tuple[list[object], list[object]]:
        asked.extend(cast(list[str], kwargs["symbols"]))
        return [SimpleNamespace(result=SimpleNamespace(symbol="^DJI"), stored_count=7)], []

    monkeypatch.setattr(run_index_daily_bars, "YfinanceAdapter", lambda **_: object())
    monkeypatch.setattr(run_index_daily_bars, "refresh_index_daily_bars", yahoo)
    monkeypatch.setattr(
        run_index_daily_bars, "emit_event", lambda _name, **fields: events.append(fields)
    )

    outcome = await run_index_daily_bars.run_refresh(
        cast(Any, _NullSessionFactory()),
        SCHEDULED_PERIOD,
        timeout_seconds=1.0,
    )
    return outcome, events[-1], asked


@pytest.mark.asyncio
async def test_the_run_is_yahoo_only_and_never_asks_for_taiex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TWSE supplies ^TWII, and everything TWSE supplies is refreshed by the
    TWSE run. This container holds no TWSE client, so its lifecycle -- the
    yfinance flag, the heartbeat, the same-day retry -- cannot reach ^TWII."""
    outcome, event, asked = await _run_refresh(monkeypatch)

    assert outcome == "complete"
    assert TAIEX_SYMBOL not in asked
    assert asked
    assert event["stored"] == 7
    assert event["failed"] == []
    assert event["succeeded"] == ["^DJI"]
    # Nothing in the event speaks for a provider this job does not use.
    assert not [key for key in event if "taiex" in key]
