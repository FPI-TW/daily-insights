from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

from daily_insights_api.modules.markets.models import IndexDailyBar
from daily_insights_api.modules.markets.service import index_moving_averages_response


def _bars(start: date, closes: list[str]) -> list[IndexDailyBar]:
    return [
        cast(
            IndexDailyBar,
            SimpleNamespace(trade_date=start + timedelta(days=index), close=Decimal(close)),
        )
        for index, close in enumerate(closes)
    ]


def test_moving_averages_have_fixed_order_and_include_current_close() -> None:
    response = index_moving_averages_response(
        symbol="^TWII",
        market_code="tw_equity",
        warmup_bars=_bars(date(2026, 1, 1), ["1"] * 19),
        requested_bars=_bars(date(2026, 1, 20), ["2"]),
    )

    assert [series.period for series in response.series] == [20, 60, 120, 240]
    assert response.method == "sma"
    assert response.price_field == "close"
    assert response.formula_version == "sma-close-v1"
    assert response.as_of == date(2026, 1, 20)
    assert response.series[0].points[0].trade_date == date(2026, 1, 20)
    assert response.series[0].points[0].value == Decimal("1.0500000000")
    assert response.series[1].points[0].value is None


def test_moving_averages_quantize_to_ten_places_with_half_even_rounding() -> None:
    response = index_moving_averages_response(
        symbol="^TWII",
        market_code="tw_equity",
        warmup_bars=_bars(date(2026, 1, 1), ["0"] * 19),
        requested_bars=_bars(date(2026, 1, 20), ["0.0000000001"]),
    )

    # 0.0000000001 / 20 is exactly 0.000000000005, halfway between
    # ten-place values.  The retained digit is even, so it rounds down.
    assert response.series[0].points[0].value == Decimal("0.0000000000")


def test_moving_averages_cross_the_requested_start_and_do_not_fill_gaps() -> None:
    response = index_moving_averages_response(
        symbol="^TWII",
        market_code="tw_equity",
        warmup_bars=_bars(date(2026, 1, 1), ["1"] * 19),
        requested_bars=[
            cast(IndexDailyBar, SimpleNamespace(trade_date=date(2026, 2, 2), close=Decimal("2"))),
            cast(IndexDailyBar, SimpleNamespace(trade_date=date(2026, 2, 5), close=Decimal("3"))),
        ],
    )

    points = response.series[0].points
    assert [point.trade_date for point in points] == [date(2026, 2, 2), date(2026, 2, 5)]
    assert [point.value for point in points] == [Decimal("1.0500000000"), Decimal("1.1500000000")]


def test_moving_averages_have_empty_fixed_series_when_no_requested_bars() -> None:
    response = index_moving_averages_response(
        symbol="^TWII",
        market_code="tw_equity",
        warmup_bars=_bars(date(2026, 1, 1), ["1"] * 239),
        requested_bars=[],
    )

    assert response.as_of is None
    assert [series.points for series in response.series] == [[], [], [], []]
