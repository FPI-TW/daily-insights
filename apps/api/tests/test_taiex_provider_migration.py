import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy.sql.elements import TextClause


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "migrations/versions/20260911_0024_taiex_switches_to_twse.py"
    spec = importlib.util.spec_from_file_location("taiex_switches_to_twse", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_deletes_only_yahoo_taiex_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration()
    statements: list[TextClause] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert len(statements) == 1
    statement = statements[0]
    assert "DELETE FROM index_daily_bars" in str(statement)
    assert statement.compile().params == {"symbol": "^TWII", "provider": "yfinance"}


def test_downgrade_preserves_valid_twse_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration()
    statements: list[TextClause] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.downgrade()

    assert statements == []
