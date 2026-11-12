import getpass
import sys

import pytest

from daily_insights_api.scripts import bootstrap_admin as bootstrap_admin_module


def test_bootstrap_cli_prompts_for_password_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    passwords = iter(["Abcd1234", "Abcd1234"])
    created: list[tuple[str, str, str]] = []

    async def create(email: str, display_name: str, password: str) -> None:
        created.append((email, display_name, password))

    monkeypatch.setattr(getpass, "getpass", lambda _: next(passwords))
    monkeypatch.setattr(bootstrap_admin_module, "bootstrap_admin", create)
    monkeypatch.setattr(
        sys,
        "argv",
        ["bootstrap-admin", "--email", "admin@example.com", "--display-name", "Admin"],
    )

    bootstrap_admin_module.main()

    assert created == [("admin@example.com", "Admin", "Abcd1234")]
    assert "Abcd1234" not in capsys.readouterr().out


def test_bootstrap_cli_rejects_mismatched_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    passwords = iter(["Abcd1234", "Different123"])
    monkeypatch.setattr(getpass, "getpass", lambda _: next(passwords))
    monkeypatch.setattr(
        sys,
        "argv",
        ["bootstrap-admin", "--email", "admin@example.com", "--display-name", "Admin"],
    )

    with pytest.raises(SystemExit):
        bootstrap_admin_module.main()
