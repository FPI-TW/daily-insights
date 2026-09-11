from datetime import date

import pytest

from daily_insights_api.modules.markets.service import (
    AUTOMATIC_SHORT_REFRESH_PERIOD,
    INITIAL_SERIES_BACKFILL_PERIOD,
    TAIEX_INCREMENTAL_MONTHS,
    TAIEX_INITIAL_BACKFILL_MONTHS,
    select_index_refresh_period,
    taiex_months,
)


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
