import pytest

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
