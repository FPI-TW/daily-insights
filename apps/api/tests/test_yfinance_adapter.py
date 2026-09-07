import asyncio
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast, get_args
from zoneinfo import ZoneInfo

import pytest
from pandas import DataFrame, DatetimeIndex, Timestamp
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.data_sources.api import (
    TRACKED_INDICES,
    IndexSymbol,
    YfinanceAdapter,
)
from daily_insights_api.modules.data_sources.errors import DataSourceContractError
from daily_insights_api.modules.data_sources.yfinance.adapter import (
    YfinanceDailyBars,
    _regular_market_end,
    normalize_daily_bars,
)
from daily_insights_api.modules.markets.api import refresh_index_daily_bars

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


def _normalize(
    frame: DataFrame,
    *,
    fetched_at: datetime = FETCHED_AT,
    regular_market_end: datetime | None = None,
) -> YfinanceDailyBars:
    return normalize_daily_bars(
        market="tw_equity",
        symbol="^TWII",
        period="2y",
        frame=frame,
        fetched_at=fetched_at,
        regular_market_end=regular_market_end,
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


def test_same_day_bar_before_regular_close_is_dropped_and_reported() -> None:
    today = date(2026, 9, 3)
    yesterday = today - timedelta(days=1)
    regular_market_end = datetime(2026, 9, 3, 13, 30, tzinfo=TAIPEI)

    result = _normalize(
        _frame(
            {
                yesterday: (100.0, 101.0, 99.0, 100.0, 1_000.0),
                today: (100.0, 100.5, 99.8, 100.2, 500.0),
            }
        ),
        fetched_at=datetime(2026, 9, 3, 12, 0, tzinfo=TAIPEI),
        regular_market_end=regular_market_end,
    )

    assert [bar.trade_date for bar in result.items] == [yesterday]
    assert result.dropped_unsettled_trade_date == today
    assert result.provenance.as_of == yesterday


@pytest.mark.parametrize(
    "fetched_at",
    [
        datetime(2026, 9, 3, 13, 30, tzinfo=TAIPEI),
        datetime(2026, 9, 3, 18, 0, tzinfo=TAIPEI),
    ],
)
def test_same_day_bar_at_or_after_regular_close_is_kept(fetched_at: datetime) -> None:
    today = date(2026, 9, 3)
    result = _normalize(
        _frame({today: (100.0, 100.5, 99.8, 100.2, 500.0)}),
        fetched_at=fetched_at,
        regular_market_end=datetime(2026, 9, 3, 13, 30, tzinfo=TAIPEI),
    )

    assert [bar.trade_date for bar in result.items] == [today]
    assert result.dropped_unsettled_trade_date is None


@pytest.mark.parametrize(
    "regular_market_end",
    [None, datetime(2026, 9, 2, 13, 30, tzinfo=TAIPEI)],
)
def test_same_day_bar_without_matching_close_metadata_fails_closed(
    regular_market_end: datetime | None,
) -> None:
    today = date(2026, 9, 3)

    with pytest.raises(DataSourceContractError, match="no settled daily bars"):
        _normalize(
            _frame({today: (100.0, 100.5, 99.8, 100.2, 500.0)}),
            fetched_at=datetime(2026, 9, 3, 18, 0, tzinfo=TAIPEI),
            regular_market_end=regular_market_end,
        )


def test_future_dated_bar_fails_closed() -> None:
    with pytest.raises(DataSourceContractError, match="no settled daily bars"):
        _normalize(
            _frame({date(2026, 9, 4): (100.0, 100.5, 99.8, 100.2, 500.0)}),
            fetched_at=datetime(2026, 9, 3, 18, 0, tzinfo=TAIPEI),
            regular_market_end=datetime(2026, 9, 3, 13, 30, tzinfo=TAIPEI),
        )


def test_regular_market_end_parses_yfinance_metadata_shape() -> None:
    end = Timestamp("2026-09-03 13:30:00", tz=TAIPEI)

    assert _regular_market_end({"currentTradingPeriod": {"regular": {"end": end}}}) == end
    assert _regular_market_end({"currentTradingPeriod": {"regular": {}}}) is None


@pytest.mark.parametrize(
    "end",
    ["2026-09-03T13:30:00+08:00", 1_788_413_400, datetime(2026, 9, 3, 13, 30)],
)
def test_regular_market_end_rejects_malformed_or_naive_values(end: object) -> None:
    assert _regular_market_end({"currentTradingPeriod": {"regular": {"end": end}}}) is None


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


def test_zero_volume_is_accepted() -> None:
    # Yahoo reports no volume for some indices (^SOX is zero on every row), so
    # zero must pass; the table's CHECK constraint allows it too.
    result = _normalize(_frame({date(2026, 9, 1): (100.0, 101.0, 99.0, 100.0, 0.0)}))

    assert result.items[0].volume == 0


def test_negative_volume_fails_closed() -> None:
    with pytest.raises(DataSourceContractError, match="with volume -1"):
        _normalize(_frame({date(2026, 9, 1): (100.0, 101.0, 99.0, 100.0, -1.0)}))


def test_the_tracked_symbol_type_and_mapping_stay_in_step() -> None:
    # mypy rejects a TRACKED_INDICES key that is not an IndexSymbol member. This
    # covers the other direction: a member declared but never mapped to a market
    # would be offered by the API and then raise on lookup.
    assert set(get_args(IndexSymbol)) == set(TRACKED_INDICES)


def test_an_untracked_symbol_is_refused_before_the_lookup() -> None:
    # refresh_index_daily_bars indexes TRACKED_INDICES directly, so an untyped
    # caller must get a named error rather than a bare KeyError.
    with pytest.raises(ValueError, match="untracked symbols: NOT_TRACKED"):
        asyncio.run(
            refresh_index_daily_bars(
                cast(AsyncSession, None),
                adapter=cast(YfinanceAdapter, None),
                symbols=[cast(IndexSymbol, "NOT_TRACKED")],
                period="7d",
            )
        )


def test_the_public_data_source_interface_does_not_import_yfinance() -> None:
    # yfinance drags in pandas and numpy, roughly 66MB and 0.2s per process.
    # data_sources.api is imported by the API and the morning-report scheduler,
    # neither of which touches Yahoo, so the import must stay inside the
    # functions that need it. A subprocess is required: this test module
    # imports pandas itself.
    probe = (
        "import sys;"
        "import daily_insights_api.modules.data_sources.api;"
        "print(sorted({'pandas', 'yfinance'} & set(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "[]"


def test_dxy_weekend_overnight_row_does_not_discard_settled_weekday_closes() -> None:
    frame = _frame(
        {
            date(2026, 9, 4): (99.03, 99.39, 98.92, 99.16, 0.0),
            date(2026, 9, 6): (99.17, 99.21, 99.08, float("nan"), 0.0),
        }
    )
    result = normalize_daily_bars(
        market="global_macro_bonds",
        symbol="DX-Y.NYB",
        period="2y",
        frame=frame,
        fetched_at=datetime(2026, 9, 7, 4, tzinfo=UTC),
    )
    assert [item.trade_date for item in result.items] == [date(2026, 9, 4)]
    assert result.provenance.record_count == 1


def test_dxy_missing_weekday_close_is_still_rejected() -> None:
    with pytest.raises(DataSourceContractError, match="without a close"):
        normalize_daily_bars(
            market="global_macro_bonds",
            symbol="DX-Y.NYB",
            period="2y",
            frame=_frame({date(2026, 9, 4): (99.03, 99.39, 98.92, float("nan"), 0.0)}),
            fetched_at=datetime(2026, 9, 7, 4, tzinfo=UTC),
        )
