from datetime import UTC, date, datetime
from typing import Any

import pytest

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom import clock
from daily_insights_api.scripts import (
    run_newsroom_assemble,
    run_newsroom_recover,
    run_newsroom_reembed,
)


def test_assemble_script_parses_an_optional_edition_date() -> None:
    assert run_newsroom_assemble.parse_args([]).edition_date is None
    options = run_newsroom_assemble.parse_args(["--edition-date", "2026-10-01"])
    assert options.edition_date == date(2026, 10, 1)


def test_assemble_script_refuses_while_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        run_newsroom_assemble, "get_settings", lambda: Settings(newsroom_enabled=False)
    )
    assert run_newsroom_assemble.main(["--edition-date", "2026-10-01"]) == 1
    assert "NEWSROOM_ENABLED" in capsys.readouterr().err


def test_assemble_script_refuses_an_open_collection_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        run_newsroom_assemble, "get_settings", lambda: Settings(newsroom_enabled=True)
    )

    def unexpected(*_: Any) -> None:
        raise AssertionError("no runtime before the window closes")

    monkeypatch.setattr(run_newsroom_assemble, "create_engine", unexpected)
    assert run_newsroom_assemble.main(["--edition-date", "2999-01-01"]) == 1
    assert "collection window" in capsys.readouterr().err


def test_reembed_script_defaults_to_the_collecting_edition() -> None:
    options = run_newsroom_reembed.parse_args([])
    assert options.start == options.end
    options = run_newsroom_reembed.parse_args(["--start", "2026-10-01", "--end", "2026-10-03"])
    assert (options.start, options.end) == (date(2026, 10, 1), date(2026, 10, 3))
    with pytest.raises(SystemExit):
        run_newsroom_reembed.parse_args(["--start", "2026-10-05", "--end", "2026-10-03"])


def test_reembed_script_refuses_while_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        run_newsroom_reembed, "get_settings", lambda: Settings(newsroom_enabled=False)
    )
    assert run_newsroom_reembed.main(["--start", "2026-10-01", "--end", "2026-10-01"]) == 1
    assert "NEWSROOM_ENABLED" in capsys.readouterr().err


def test_recover_script_parses_commands() -> None:
    options = run_newsroom_recover.parse_args(
        ["requeue", "--stage", "en", "--error-code", "a", "--error-code", "b", "--to", "2026-10-27"]
    )
    assert (options.stage, options.error_codes, options.start, options.end, options.apply) == (
        "en",
        ["a", "b"],
        None,
        date(2026, 10, 27),
        False,
    )
    with pytest.raises(SystemExit):
        run_newsroom_recover.parse_args(["requeue", "--stage", "editor", "--error-code", "a"])
    with pytest.raises(SystemExit):
        run_newsroom_recover.parse_args(["requeue", "--stage", "embed"])
    with pytest.raises(SystemExit):
        run_newsroom_recover.parse_args(
            ["reembed-untranslated", "--from", "2026-10-27", "--to", "2026-10-25"]
        )
    options = run_newsroom_recover.parse_args(
        ["reopen", "--edition-date", "2026-10-25", "--extend-late-fill", "18:30"]
    )
    deadline = options.late_fill_deadline.astimezone(clock.TAIPEI)
    assert (deadline.date(), deadline.hour, deadline.minute) == (clock.taipei_today(), 18, 30)
    options = run_newsroom_recover.parse_args(
        ["reopen", "--edition-date", "2026-10-25", "--extend-late-fill", "2026-10-28T01:00+00:00"]
    )
    assert options.late_fill_deadline == datetime(2026, 10, 28, 1, tzinfo=UTC)
    with pytest.raises(SystemExit):
        run_newsroom_recover.parse_args(
            ["reopen", "--edition-date", "2026-10-25", "--extend-late-fill", "soon"]
        )
