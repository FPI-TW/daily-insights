from datetime import date
from typing import Any

import pytest

from daily_insights_api.core.config import Settings
from daily_insights_api.scripts import run_newsroom_assemble


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
