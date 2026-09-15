import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import BigInteger, Column


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "migrations/versions/20260915_0026_taiex_trade_value.py"
    spec = importlib.util.spec_from_file_location("taiex_trade_value", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_nullable_nonnegative_trade_value(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration()
    added: list[tuple[str, Column[object]]] = []
    checks: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        migration.op, "add_column", lambda table, column: added.append((table, column))
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: checks.append((name, table, condition)),
    )

    migration.upgrade()

    assert len(added) == 1
    table, column = added[0]
    assert table == "index_daily_bars"
    assert column.name == "trade_value"
    assert isinstance(column.type, BigInteger)
    assert column.nullable is True
    assert checks == [
        (
            "ck_index_daily_bars_trade_value_nonnegative",
            "index_daily_bars",
            "trade_value IS NULL OR trade_value >= 0",
        )
    ]


def test_downgrade_removes_only_trade_value(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table, **kwargs: calls.append(("constraint", name, table, kwargs)),
    )
    monkeypatch.setattr(
        migration.op, "drop_column", lambda table, column: calls.append(("column", table, column))
    )

    migration.downgrade()

    assert calls == [
        (
            "constraint",
            "ck_index_daily_bars_trade_value_nonnegative",
            "index_daily_bars",
            {"type_": "check"},
        ),
        ("column", "index_daily_bars", "trade_value"),
    ]
