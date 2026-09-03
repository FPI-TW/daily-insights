from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pandas import DataFrame, DatetimeIndex, Timestamp

from daily_insights_api.modules.data_sources.errors import DataSourceContractError
from daily_insights_api.modules.data_sources.yfinance.adapter import (
    DailyBarsResult,
    normalize_daily_bars,
)

TAIPEI = ZoneInfo("Asia/Taipei")
FETCHED_AT = datetime(2026, 9, 3, 4, 0, tzinfo=UTC)


def _frame(rows: dict[date, tuple[float, float, float, float, float]]) -> DataFrame:
    index = DatetimeIndex([Timestamp(day, tz=TAIPEI) for day in rows])
    columns = ("Open", "High", "Low", "Close", "Volume")
    return DataFrame(
        {
            name: [values[position] for values in rows.values()]
            for position, name in enumerate(columns)
        },
        index=index,
    )


def _normalize(frame: DataFrame) -> DailyBarsResult:
    return normalize_daily_bars(
        market="tw_equity",
        symbol="^TWII",
        period="2y",
        frame=frame,
        fetched_at=FETCHED_AT,
    )


def test_settled_bars_are_mapped_with_provenance() -> None:
    result = _normalize(
        _frame(
            {
                date(2026, 8, 31): (100.5, 101.0, 99.5, 100.0, 1_000.0),
                date(2026, 9, 1): (100.0, 102.0, 100.0, 101.25, 2_000.0),
            }
        )
    )

    assert [bar.trade_date for bar in result.items] == [date(2026, 8, 31), date(2026, 9, 1)]
    assert result.items[1].close == Decimal("101.25")
    assert result.items[1].volume == 2_000
    assert result.items[0].symbol == "^TWII"
    assert result.items[0].market == "tw_equity"
    assert result.dropped_unsettled_trade_date is None
    assert result.provenance.provider == "yfinance"
    assert result.provenance.as_of == date(2026, 9, 1)
    assert result.provenance.record_count == 2


def test_todays_unsettled_bar_is_dropped_and_reported() -> None:
    today = datetime.now(TAIPEI).date()
    yesterday = today - timedelta(days=1)

    result = _normalize(
        _frame(
            {
                yesterday: (100.0, 101.0, 99.0, 100.0, 1_000.0),
                today: (100.0, 100.5, 99.8, 100.2, 500.0),
            }
        )
    )

    assert [bar.trade_date for bar in result.items] == [yesterday]
    assert result.dropped_unsettled_trade_date == today
    assert result.provenance.as_of == yesterday


def test_only_an_unsettled_bar_fails_closed() -> None:
    today = datetime.now(TAIPEI).date()

    with pytest.raises(DataSourceContractError, match="no settled daily bars"):
        _normalize(_frame({today: (100.0, 100.5, 99.8, 100.2, 500.0)}))


def test_missing_column_fails_closed() -> None:
    frame = _frame({date(2026, 9, 1): (100.0, 101.0, 99.0, 100.0, 1_000.0)}).drop(
        columns=["Volume"]
    )

    with pytest.raises(DataSourceContractError, match="omitted columns: Volume"):
        _normalize(frame)


def test_settled_row_without_a_close_fails_closed() -> None:
    with pytest.raises(DataSourceContractError, match="without a close"):
        _normalize(_frame({date(2026, 9, 1): (100.0, 101.0, 99.0, float("nan"), 1_000.0)}))


def test_nonpositive_close_fails_closed() -> None:
    # Mirrors the index_daily_bars CHECK constraint: rejecting the bar here keeps
    # a bad row from reaching the database and failing the whole batch on write.
    with pytest.raises(DataSourceContractError, match="with close 0"):
        _normalize(_frame({date(2026, 9, 1): (100.0, 101.0, 99.0, 0.0, 1_000.0)}))


def test_high_below_low_fails_closed() -> None:
    with pytest.raises(DataSourceContractError, match=r"high 98\.0 below low 99\.0"):
        _normalize(_frame({date(2026, 9, 1): (100.0, 98.0, 99.0, 100.0, 1_000.0)}))
