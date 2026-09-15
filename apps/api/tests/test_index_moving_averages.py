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
            SimpleNamespace(
                trade_date=start + timedelta(days=index),
                high=Decimal(close),
                low=Decimal(close),
                close=Decimal(close),
            ),
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
    assert response.rsi.period == 14
    assert response.rsi.points[0].value == Decimal("100.0000000000")
    assert response.macd.fast_period == 12
    assert response.macd.slow_period == 26
    assert response.macd.signal_period == 9
    assert response.kd.lookback_period == 9
    assert response.kd.k_smoothing_period == 3
    assert response.kd.d_smoothing_period == 3


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
            cast(
                IndexDailyBar,
                SimpleNamespace(
                    trade_date=date(2026, 2, 2),
                    high=Decimal("2"),
                    low=Decimal("2"),
                    close=Decimal("2"),
                ),
            ),
            cast(
                IndexDailyBar,
                SimpleNamespace(
                    trade_date=date(2026, 2, 5),
                    high=Decimal("3"),
                    low=Decimal("3"),
                    close=Decimal("3"),
                ),
            ),
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
    assert response.rsi.points == []
    assert response.macd.points == []
    assert response.kd.points == []


def test_rsi_and_macd_use_hidden_warmup_and_emit_neutral_constant_series() -> None:
    response = index_moving_averages_response(
        symbol="^GSPC",
        market_code="us_equity",
        warmup_bars=_bars(date(2026, 1, 1), ["100"] * 34),
        requested_bars=_bars(date(2026, 2, 4), ["100"]),
    )

    assert response.rsi.method == "wilder"
    assert response.rsi.formula_version == "rsi-wilder-close-v1"
    assert response.rsi.points[0].value == Decimal("50.0000000000")
    assert response.macd.method == "ema"
    assert response.macd.formula_version == "macd-ema-close-v1"
    point = response.macd.points[0]
    assert point.macd == Decimal("0E-10")
    assert point.signal == Decimal("0E-10")
    assert point.histogram == Decimal("0E-10")


def test_rsi_and_macd_follow_a_steady_rising_series() -> None:
    response = index_moving_averages_response(
        symbol="^GSPC",
        market_code="us_equity",
        warmup_bars=_bars(date(2026, 1, 1), [str(value) for value in range(1, 35)]),
        requested_bars=_bars(date(2026, 2, 4), ["35"]),
    )

    assert response.rsi.points[0].value == Decimal("100.0000000000")
    point = response.macd.points[0]
    assert point.macd == Decimal("7.0000000000")
    assert point.signal == Decimal("7.0000000000")
    assert point.histogram == Decimal("0E-10")


def test_kd_uses_hidden_warmup_and_taiwan_smoothing() -> None:
    warmup = [
        cast(
            IndexDailyBar,
            SimpleNamespace(
                trade_date=date(2026, 1, day),
                high=Decimal(day),
                low=Decimal(day),
                close=Decimal(day),
            ),
        )
        for day in range(1, 10)
    ]
    requested = [
        cast(
            IndexDailyBar,
            SimpleNamespace(
                trade_date=date(2026, 1, 10),
                high=Decimal(10),
                low=Decimal(10),
                close=Decimal(10),
            ),
        )
    ]

    response = index_moving_averages_response(
        symbol="^VIX",
        market_code="us_equity",
        warmup_bars=warmup,
        requested_bars=requested,
    )

    assert response.kd.method == "smoothed-rsv"
    assert response.kd.formula_version == "stochastic-kd-9-3-3-v1"
    assert response.kd.points[0].k == Decimal("77.7777777778")
    assert response.kd.points[0].d == Decimal("62.9629629630")


def test_kd_returns_null_when_high_or_low_is_missing_in_lookback() -> None:
    bars = _bars(date(2026, 1, 1), ["10"] * 9)
    bars[4].high = None

    response = index_moving_averages_response(
        symbol="^VIX",
        market_code="us_equity",
        warmup_bars=[],
        requested_bars=bars,
    )

    assert response.kd.points[-1].k is None
    assert response.kd.points[-1].d is None


def test_kd_clamps_invalid_source_ranges_to_oscillator_bounds() -> None:
    bars = [
        cast(
            IndexDailyBar,
            SimpleNamespace(
                trade_date=date(2026, 1, day),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("1"),
            ),
        )
        for day in range(1, 10)
    ]

    response = index_moving_averages_response(
        symbol="^VIX",
        market_code="us_equity",
        warmup_bars=[],
        requested_bars=bars,
    )

    assert response.kd.points[-1].k == Decimal("33.3333333333")
    assert response.kd.points[-1].d == Decimal("44.4444444444")
