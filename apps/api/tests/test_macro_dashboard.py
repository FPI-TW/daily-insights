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
    requested_dates: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested_dates.append(request.url.params["date"])
        day = request.url.params["date"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "rows": [
                        {
                            "gmt": "17:00" if day == "2026-09-03" else "12:00",
                            "country": "United States",
                            "eventName": "Released" if day == "2026-09-03" else "Future",
                            "actual": "0%" if day == "2026-09-03" else "99%",
                            "consensus": "1.2%",
                            "previous": "1.0%",
                        },
                        *(
                            [
                                {
                                    "gmt": "17:00",
                                    "country": "United States",
                                    "eventName": "Tomorrow",
                                    "actual": "",
                                }
                            ]
                            if day == "2026-09-04"
                            else []
                        ),
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await macro.load_calendar(client, datetime(2026, 9, 4, tzinfo=UTC))
    assert result.status == "ok"
    assert result.source == "Nasdaq"
    assert requested_dates == ["2026-09-03", "2026-09-04"]
    assert [event.event for event in result.events] == ["Released", "Future"]
    assert result.events[0].actual == 0
    assert result.events[1].actual is None
    assert result.events[0].unit == "%"
    assert result.events[0].country == "US"
    assert result.events[0].currency == "USD"
    assert result.events[0].date.tzinfo is not None


async def test_denied_calendar_is_not_a_successful_empty_day() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        denied = await macro.load_calendar(client, datetime.now(UTC))
        assert denied.status == "unavailable"
        assert denied.source == "Nasdaq"
        assert calls == 2


async def test_calendar_keeps_a_successful_day_when_the_other_request_fails() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params["date"] == "2026-09-03":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "data": {
                    "rows": [
                        {
                            "gmt": "01:00",
                            "country": "Japan",
                            "eventName": "Leading Index",
                            "actual": "118.1",
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await macro.load_calendar(client, datetime(2026, 9, 4, tzinfo=UTC))

    assert result.status == "ok"
    assert [(event.country, event.event) for event in result.events] == [("JP", "Leading Index")]


def test_calendar_value_parser_preserves_scale_and_rejects_placeholders() -> None:
    assert macro.parse_calendar_value("357,050.0M") == (Decimal("357050.0"), "M")
    assert macro.parse_calendar_value("$1.2B") == (Decimal("1.2"), "USD B")
    assert macro.parse_calendar_value("&nbsp;") == (None, None)
    assert macro.parse_calendar_value("N/A") == (None, None)


async def test_disabled_providers_make_no_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(**kwargs: object) -> None:
        raise AssertionError("disabled provider was instantiated")

    monkeypatch.setattr(macro, "YfinanceAdapter", unexpected)
    monkeypatch.setattr(macro, "TwelveDataTransport", unexpected)
    histories = await macro.load_market_histories(
        Settings(yfinance_enabled=False, twelve_data_api_key=None)
    )
    assert len(histories) == len(macro.COMMODITIES) + len(macro.INSTRUMENTS)
    assert all(item.status == "disabled" for item in histories)
    assert [item.source for item in histories[: len(macro.COMMODITIES)]] == ["Twelve Data"] * len(
        macro.COMMODITIES
    )


async def test_commodities_use_twelve_data_spot_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    calls: list[dict[str, object]] = []

    class FakeTransport:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["api_key"].get_secret_value() == "key"  # type: ignore[attr-defined]

        async def __aenter__(self) -> "FakeTransport":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeAdapter:
        def __init__(self, transport: object) -> None:
            assert isinstance(transport, FakeTransport)

        async def get_daily_bars(self, **kwargs: object) -> object:
            calls.append(kwargs)
            if kwargs["symbol"] == "HG1":
                raise RuntimeError("provider down")
            return SimpleNamespace(
                items=(
                    SimpleNamespace(trade_date=date(2026, 9, 3), close=Decimal("100")),
                    SimpleNamespace(trade_date=date(2026, 9, 4), close=Decimal("101")),
                )
            )

    monkeypatch.setattr(macro, "TwelveDataTransport", FakeTransport)
    monkeypatch.setattr(macro, "TwelveDataAdapter", FakeAdapter)
    histories = await macro.load_commodity_histories(Settings(twelve_data_api_key=SecretStr("key")))
    assert [item.symbol for item in histories] == [
        "XBR/USD",
        "WTI/USD",
        "XAU/USD",
        "XAG/USD",
        "HG1",
    ]
    assert all(call["symbol_type"] == "commodity" for call in calls)
    assert all(call["expected_currency"] == "USD" for call in calls)
    assert all(call["outputsize"] == macro.COMMODITY_HISTORY for call in calls)
    assert [item.status for item in histories] == ["ok", "ok", "ok", "ok", "unavailable"]
    assert all(item.source == "Twelve Data" for item in histories)
    assert histories[0].points[-1].value == Decimal("101")


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
            status="disabled",
            date=datetime.now(ZoneInfo("Asia/Taipei")).date(),
            source="Nasdaq",
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
