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


class _StoredDates:
    def __init__(self, dates: list[date]) -> None:
        self._dates = dates

    def all(self) -> list[date]:
        return self._dates


class _Database:
    def __init__(self, dates: list[date]) -> None:
        self._dates = dates

    async def scalars(self, _: object) -> _StoredDates:
        return _StoredDates(self._dates)


class _WriteSessionFactory:
    def begin(self) -> "_WriteSessionFactory":
        return self

    async def __aenter__(self) -> "_WriteSessionFactory":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, _: object) -> None:
        return None


@pytest.mark.parametrize(
    ("period", "has_stored_bars", "expected"),
    [
        (AUTOMATIC_SHORT_REFRESH_PERIOD, False, INITIAL_SERIES_BACKFILL_PERIOD),
        (AUTOMATIC_SHORT_REFRESH_PERIOD, True, AUTOMATIC_SHORT_REFRESH_PERIOD),
        # Only the incremental period bootstraps a missing series; every other
        # window is an explicit request and passes through untouched.
        ("2y", False, "2y"),
        ("5y", False, "5y"),
        ("max", False, "max"),
    ],
)
def test_select_index_refresh_period(
    period: str,
    has_stored_bars: bool,
    expected: str,
) -> None:
    assert select_index_refresh_period(period=period, has_stored_bars=has_stored_bars) == expected


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
