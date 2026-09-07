from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.reports import macro_dashboard as macro


def test_treasury_preserves_dates_zero_yields_and_missing_tenors() -> None:
    payload = b"""<feed xmlns:m="urn:meta" xmlns:d="urn:data">
      <m:properties><d:NEW_DATE>2026-09-03T00:00:00</d:NEW_DATE>
      <d:BC_3MONTH>0.00</d:BC_3MONTH><d:BC_2YEAR>3.98</d:BC_2YEAR></m:properties>
      <m:properties><d:NEW_DATE>2026-09-04T00:00:00</d:NEW_DATE>
      <d:BC_2YEAR>4.01</d:BC_2YEAR></m:properties>
      <m:properties><d:NEW_DATE>2099-09-04T00:00:00</d:NEW_DATE>
      <d:BC_2YEAR>99</d:BC_2YEAR></m:properties></feed>"""
    histories = macro.treasury_histories([payload], date(2026, 9, 4))
    assert histories[0].points[0].value == Decimal("0")
    assert [point.value for point in histories[1].points] == [Decimal("3.98"), Decimal("4.01")]
    assert histories[2].status == "unavailable"
    assert histories[2].points == []


async def test_treasury_invalid_feed_degrades_without_inventing_a_curve() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"not XML"))
    ) as client:
        histories = await macro.load_treasury(client, date(2026, 9, 4))
    assert all(item.status == "unavailable" and not item.points for item in histories)


async def test_sofr_validates_type_and_sorts_observations() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "refRates": [
                        {"effectiveDate": "2026-09-04", "percentRate": 3.66, "type": "SOFR"},
                        {"effectiveDate": "2026-09-03", "percentRate": 3.65, "type": "SOFR"},
                    ]
                },
            )
        )
    ) as client:
        result = await macro.load_sofr(client, date(2026, 9, 4))
    assert [point.date for point in result.points] == [date(2026, 9, 3), date(2026, 9, 4)]
    assert result.points[-1].value == Decimal("3.66")


async def test_calendar_filters_taipei_day_and_preserves_actual_zero() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["from"] == "2026-09-03"
        return httpx.Response(
            200,
            json=[
                {"date": "2026-09-03 17:00:00", "country": "US", "event": "Released", "actual": 0},
                {"date": "2026-09-04 12:00:00", "country": "US", "event": "Future", "actual": 99},
                {
                    "date": "2026-09-04 17:00:00",
                    "country": "US",
                    "event": "Tomorrow",
                    "actual": None,
                },
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await macro.load_calendar(
            client, Settings(fmp_api_key=SecretStr("test")), datetime(2026, 9, 4, tzinfo=UTC)
        )
    assert result.status == "ok"
    assert [event.event for event in result.events] == ["Released", "Future"]
    assert result.events[0].actual == 0
    assert result.events[1].actual is None
    assert result.events[0].date.tzinfo is not None


async def test_disabled_and_denied_calendar_are_not_a_successful_empty_day() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        disabled = await macro.load_calendar(client, Settings(fmp_api_key=None), datetime.now(UTC))
        assert disabled.status == "disabled"
        assert calls == 0
        denied = await macro.load_calendar(
            client, Settings(fmp_api_key=SecretStr("test")), datetime.now(UTC)
        )
        assert denied.status == "unavailable"
        assert calls == 1


async def test_disabled_yahoo_makes_no_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(**kwargs: object) -> None:
        raise AssertionError("disabled provider was instantiated")

    monkeypatch.setattr(macro, "YfinanceAdapter", unexpected)
    histories = await macro.load_market_histories(Settings(yfinance_enabled=False))
    assert len(histories) == len(macro.INSTRUMENTS)
    assert all(item.status == "disabled" for item in histories)


async def test_cache_coalesces_concurrent_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    markets = AsyncMock(return_value=[])
    treasury = AsyncMock(return_value=[])
    sofr = AsyncMock(
        return_value=macro.History(
            id="sofr", symbol="SOFR", unit="percent", source="New York Fed", status="unavailable"
        )
    )
    calendar = AsyncMock(
        return_value=macro.Calendar(
            status="disabled", date=datetime.now(ZoneInfo("Asia/Taipei")).date()
        )
    )
    monkeypatch.setattr(macro, "load_market_histories", markets)
    monkeypatch.setattr(macro, "load_treasury", treasury)
    monkeypatch.setattr(macro, "load_sofr", sofr)
    monkeypatch.setattr(macro, "load_calendar", calendar)
    service = macro.MacroDashboardService(Settings())
    first, second = await asyncio.gather(service.get(), service.get())
    assert first is second
    markets.assert_awaited_once()
    treasury.assert_awaited_once()


async def test_dashboard_authorizes_before_reading_shared_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from typing import cast

    from fastapi import HTTPException, Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from daily_insights_api.modules.identity.api import AuthContext
    from daily_insights_api.modules.reports import router

    service = SimpleNamespace(get=AsyncMock())
    request = Request(
        {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(macro_dashboard=service))}
    )
    monkeypatch.setattr(
        router, "visible_report_market_codes", AsyncMock(return_value=frozenset({"forex"}))
    )
    with pytest.raises(HTTPException) as error:
        await router.get_macro_dashboard(request, cast(AuthContext, None), cast(AsyncSession, None))
    assert error.value.status_code == 404
    service.get.assert_not_awaited()
