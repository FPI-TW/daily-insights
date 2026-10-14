from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from daily_insights_api.modules.data_sources.api import TaiexDailyBar, TaiexDailyBars, TwseAdapter
from daily_insights_api.modules.markets import service as markets_service
from daily_insights_api.modules.markets.service import (
    AUTOMATIC_SHORT_REFRESH_PERIOD,
    INITIAL_SERIES_BACKFILL_PERIOD,
    TAIEX_INCREMENTAL_MONTHS,
    TAIEX_INITIAL_BACKFILL_MONTHS,
    refresh_taiex_daily_bars,
    select_index_refresh_period,
    select_taiex_refresh_months,
    taiex_months,
)


class _StoredRows:
    def __init__(self, rows: list[tuple[date, int | None]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[date, int | None]]:
        return self._rows


class _Database:
    def __init__(self, dates: list[date], *, incomplete: frozenset[date] = frozenset()) -> None:
        self._rows = [(value, None if value in incomplete else 1) for value in dates]

    async def execute(self, _: object) -> _StoredRows:
        return _StoredRows(self._rows)


class _WriteSessionFactory:
    def begin(self) -> "_WriteSessionFactory":
        return self

    async def __aenter__(self) -> "_WriteSessionFactory":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, _: object) -> None:
        return None


TODAY = date(2026, 10, 13)


@pytest.mark.parametrize(
    ("period", "latest_bar_date", "expected"),
    [
        (AUTOMATIC_SHORT_REFRESH_PERIOD, None, INITIAL_SERIES_BACKFILL_PERIOD),
        # Yesterday, a long weekend, and exactly a week all stay in the short window.
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2026, 10, 12), AUTOMATIC_SHORT_REFRESH_PERIOD),
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2026, 10, 9), AUTOMATIC_SHORT_REFRESH_PERIOD),
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2026, 10, 6), AUTOMATIC_SHORT_REFRESH_PERIOD),
        # A gap the short window cannot heal widens to the smallest covering period.
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2026, 10, 5), "1mo"),
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2026, 9, 15), "1mo"),
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2026, 9, 14), "3mo"),
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2026, 7, 21), "3mo"),
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2026, 7, 20), "6mo"),
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2026, 4, 28), "6mo"),
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2026, 4, 27), "1y"),
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2025, 10, 28), "1y"),
        (AUTOMATIC_SHORT_REFRESH_PERIOD, date(2025, 10, 27), INITIAL_SERIES_BACKFILL_PERIOD),
        # Only the incremental period bootstraps a missing series or widens for
        # a gap; every other window is an explicit request and passes through.
        ("2y", None, "2y"),
        ("5y", None, "5y"),
        ("max", None, "max"),
        ("1d", date(2026, 1, 1), "1d"),
    ],
)
def test_select_index_refresh_period(
    period: str,
    latest_bar_date: date | None,
    expected: str,
) -> None:
    assert (
        select_index_refresh_period(period=period, latest_bar_date=latest_bar_date, today=TODAY)
        == expected
    )


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        # A month is addressed by any date inside it.
        (date(2026, 9, 1), date(2026, 9, 30), [date(2026, 9, 1)]),
        (date(2026, 8, 15), date(2026, 9, 11), [date(2026, 8, 1), date(2026, 9, 1)]),
        # Year boundary: the walk must not land on month 13.
        (
            date(2025, 12, 3),
            date(2026, 2, 1),
            [date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)],
        ),
        # A 31st steps back to a 30-day month without overflowing.
        (
            date(2026, 1, 31),
            date(2026, 3, 31),
            [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)],
        ),
    ],
)
def test_taiex_months_walks_whole_months(start: date, end: date, expected: list[date]) -> None:
    assert list(taiex_months(start=start, end=end)) == expected


def test_taiex_months_rejects_a_backwards_range() -> None:
    with pytest.raises(ValueError, match="start must not be after end"):
        taiex_months(start=date(2026, 9, 2), end=date(2026, 9, 1))


def test_taiex_backfill_is_two_years_counted_in_months() -> None:
    # The Yahoo path bootstraps an empty series with "2y"; this is the same
    # span counted the way TWSE is addressed.
    assert TAIEX_INITIAL_BACKFILL_MONTHS == 25
    assert INITIAL_SERIES_BACKFILL_PERIOD == "2y"
    months = taiex_months(start=date(2024, 9, 1), end=date(2026, 9, 11))
    assert len(months) == TAIEX_INITIAL_BACKFILL_MONTHS
    assert TAIEX_INCREMENTAL_MONTHS < TAIEX_INITIAL_BACKFILL_MONTHS


@pytest.mark.asyncio
async def test_incremental_taiex_selection_prioritizes_recent_then_missing_history() -> None:
    today = date(2026, 9, 11)
    required = taiex_months(start=date(2024, 9, 1), end=today)
    missing = {date(2024, 9, 1), date(2024, 10, 1)}
    stored = [month for month in required if month not in missing]

    selected = await select_taiex_refresh_months(
        cast(Any, _Database(stored)),
        today=today,
        requested_months=TAIEX_INCREMENTAL_MONTHS,
    )

    assert selected == (
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2024, 9, 1),
        date(2024, 10, 1),
    )


@pytest.mark.asyncio
async def test_empty_taiex_selection_requests_all_25_months() -> None:
    selected = await select_taiex_refresh_months(
        cast(Any, _Database([])),
        today=date(2026, 9, 11),
        requested_months=TAIEX_INCREMENTAL_MONTHS,
    )

    assert len(selected) == TAIEX_INITIAL_BACKFILL_MONTHS
    assert selected[:3] == (date(2026, 8, 1), date(2026, 9, 1), date(2024, 9, 1))


@pytest.mark.asyncio
async def test_taiex_selection_retries_a_month_with_missing_trade_value() -> None:
    today = date(2026, 9, 11)
    required = taiex_months(start=date(2024, 9, 1), end=today)
    incomplete = date(2025, 8, 1)

    selected = await select_taiex_refresh_months(
        cast(Any, _Database(list(required), incomplete=frozenset({incomplete}))),
        today=today,
        requested_months=TAIEX_INCREMENTAL_MONTHS,
    )

    assert selected[:2] == (date(2026, 8, 1), date(2026, 9, 1))
    assert selected[2:] == (incomplete,)


@pytest.mark.asyncio
async def test_empty_taiex_month_is_reported_as_retryable_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_at = datetime(2026, 9, 11, tzinfo=UTC)

    class Adapter:
        async def get_taiex_daily_bars(self, month: date) -> TaiexDailyBars:
            items: tuple[TaiexDailyBar, ...] = ()
            if month == date(2026, 9, 1):
                items = (
                    TaiexDailyBar(
                        trade_date=month,
                        open=Decimal("100"),
                        high=Decimal("101"),
                        low=Decimal("99"),
                        close=Decimal("100"),
                        volume=1_000,
                        trade_value=2_000,
                    ),
                )
            return TaiexDailyBars(month=month, items=items, fetched_at=fetched_at)

    async def store(*_: object, **kwargs: object) -> int:
        return len(cast(list[object], kwargs["bars"]))

    monkeypatch.setattr(markets_service, "store_index_daily_bars", store)
    refreshed = await refresh_taiex_daily_bars(
        cast(Any, _WriteSessionFactory()),
        adapter=cast(TwseAdapter, Adapter()),
        months=(date(2026, 8, 1), date(2026, 9, 1)),
    )

    assert refreshed.stored_count == 1
    assert refreshed.failed_months == ("2026-08: no data",)
