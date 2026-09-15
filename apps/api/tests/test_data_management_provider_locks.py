from datetime import date
from typing import Any, cast

import pytest

from daily_insights_api.modules.admin import router as admin_router
from daily_insights_api.modules.data_management import service as data_management_service
from daily_insights_api.modules.data_sources.api import TwelveDataAdapter
from daily_insights_api.modules.markets import service as markets_service
from daily_insights_api.modules.reports import morning_report
from daily_insights_api.modules.reports.api import LaunchMarketCode


class _LockSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def execute(self, statement: object) -> None:
        rendered = str(statement)
        if "pg_advisory_lock" in rendered:
            self.events.append("lock")
        elif "pg_advisory_unlock" in rendered:
            self.events.append("unlock")

    async def scalar(self, _: object) -> None:
        self.events.append("publication-check")
        return None

    async def rollback(self) -> None:
        return None


class _SessionFactory:
    def __init__(self, events: list[str]) -> None:
        self.session = _LockSession(events)

    def __call__(self) -> "_SessionFactory":
        return self

    async def __aenter__(self) -> _LockSession:
        return self.session

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.mark.asyncio
async def test_morning_manual_and_scheduled_paths_take_the_same_lock_before_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    keys: list[tuple[str, str, date]] = []
    factory = _SessionFactory(events)

    def lock_key(report_key: str, market_code: str, edition_date: date) -> int:
        keys.append((report_key, market_code, edition_date))
        return 42

    async def provider_work(*_: object, **__: object) -> Any:
        assert events[-1] in {"lock", "publication-check"}
        events.append("provider")
        return cast(Any, None)

    monkeypatch.setattr(morning_report, "_provider_lock_key", lock_key)
    monkeypatch.setattr(morning_report, "_run_market_unlocked", provider_work)
    market: LaunchMarketCode = "crypto"
    edition = date(2026, 9, 7)

    await morning_report._run_market(
        cast(Any, factory), cast(TwelveDataAdapter, object()), market, edition
    )
    assert events == ["lock", "provider", "unlock"]

    events.clear()
    await morning_report._run_scheduled_market(
        cast(Any, factory), cast(TwelveDataAdapter, object()), market, edition
    )
    assert events == ["lock", "publication-check", "provider", "unlock"]
    assert keys == [("daily-market", "crypto", edition)] * 2


@pytest.mark.asyncio
async def test_yahoo_shared_lock_wraps_work_and_releases_after_success_or_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    database = _LockSession(events)

    async def succeeds(*_: object, **__: object) -> tuple[list[object], list[object]]:
        assert events == ["lock"]
        events.append("provider")
        return [], []

    monkeypatch.setattr(markets_service, "_refresh_index_daily_bars_unlocked", succeeds)
    await markets_service.refresh_index_daily_bars(
        cast(Any, database),
        adapter=cast(Any, object()),
        symbols=["^DJI"],
        period="7d",
    )
    assert events == ["lock", "provider", "unlock"]
    assert (
        cast(Any, data_management_service).refresh_index_daily_bars
        is markets_service.refresh_index_daily_bars
    )
    assert (
        cast(Any, admin_router).refresh_index_daily_bars is markets_service.refresh_index_daily_bars
    )

    events.clear()

    async def fails(*_: object, **__: object) -> tuple[list[object], list[object]]:
        assert events == ["lock"]
        events.append("provider")
        raise RuntimeError("upstream")

    monkeypatch.setattr(markets_service, "_refresh_index_daily_bars_unlocked", fails)
    with pytest.raises(RuntimeError, match="upstream"):
        await markets_service.refresh_index_daily_bars(
            cast(Any, database),
            adapter=cast(Any, object()),
            symbols=["^DJI"],
            period="7d",
        )
    assert events == ["lock", "provider", "unlock"]
