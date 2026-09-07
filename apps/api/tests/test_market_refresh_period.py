import pytest

from daily_insights_api.modules.markets.service import select_index_refresh_period


@pytest.mark.parametrize(
    ("period", "has_stored_bars", "expected"),
    [
        ("7d", False, "2y"),
        ("7d", True, "7d"),
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
