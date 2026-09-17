import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

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


async def test_treasury_fetches_years_sequentially() -> None:
    active = 0
    maximum = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(200, content=b"<feed />", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await macro.load_treasury(client, date(2026, 9, 17))

    assert maximum == 1


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


async def test_disabled_providers_make_no_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(**kwargs: object) -> None:
        raise AssertionError("disabled provider was instantiated")

    monkeypatch.setattr(macro, "YfinanceAdapter", unexpected)
    monkeypatch.setattr(macro, "TwelveDataTransport", unexpected)
    histories = await macro.load_market_histories(
        Settings(yfinance_enabled=False, twelve_data_api_key=None)
    )
    assert len(histories) == len(macro.COMMODITIES) + len(macro.INSTRUMENTS) + len(
        macro.FX_INSTRUMENTS
    )
    assert all(item.status == "disabled" for item in histories)
    assert [item.source for item in histories[: len(macro.COMMODITIES)]] == ["Twelve Data"] * len(
        macro.COMMODITIES
    )


async def test_fx_histories_use_twelve_data_and_publish_base_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    calls: list[dict[str, object]] = []

    class FakeAdapter:
        def __init__(self, transport: object) -> None:
            assert isinstance(transport, _FakeTransport)

        async def get_daily_bars(self, **kwargs: object) -> object:
            calls.append(kwargs)
            symbol = kwargs["symbol"]
            assert isinstance(symbol, str)
            return SimpleNamespace(
                items=(
                    _bar(symbol, date(2025, 9, 5), Decimal("100")),
                    _bar(symbol, date(2026, 8, 10), Decimal("110")),
                    _bar(symbol, date(2026, 9, 4), Decimal("120")),
                )
            )

    monkeypatch.setattr(macro, "TwelveDataTransport", _FakeTransport)
    monkeypatch.setattr(macro, "TwelveDataAdapter", FakeAdapter)
    histories = await macro.load_fx_histories(Settings(twelve_data_api_key=SecretStr("key")))

    assert [history.symbol for history in histories] == [item[1] for item in macro.FX_INSTRUMENTS]
    assert all(history.source == "Twelve Data" and history.status == "ok" for history in histories)
    assert all(
        history.base_dates
        == {"30": date(2026, 8, 10), "90": date(2026, 8, 10), "365": date(2025, 9, 5)}
        for history in histories
    )
    assert all(
        call["outputsize"] == 400
        and isinstance(call["end_date"], date)
        and call["timezone"] == "Australia/Sydney"
        for call in calls
    )


def test_fx_provider_end_date_uses_the_requested_twelve_data_timezone() -> None:
    assert macro.fx_provider_end_date(datetime(2026, 9, 4, 15, 30, tzinfo=UTC)) == date(2026, 9, 5)


def _eod(symbol: str, as_of: date, close: Decimal) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(symbol=symbol, currency="USD", as_of=as_of, close=close)


def _bar(symbol: str, trade_date: date, close: Decimal) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(symbol=symbol, trade_date=trade_date, close=close)


class _FakeTransport:
    def __init__(self, **kwargs: object) -> None:
        assert kwargs["api_key"].get_secret_value() == "key"  # type: ignore[attr-defined]

    async def __aenter__(self) -> "_FakeTransport":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


async def test_commodities_cut_twelve_data_spot_history_at_eod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    eod_date = date(2026, 9, 4)
    eod_calls: list[dict[str, object]] = []
    bar_calls: list[dict[str, object]] = []

    class FakeAdapter:
        def __init__(self, transport: object) -> None:
            assert isinstance(transport, _FakeTransport)

        async def get_eods(self, **kwargs: object) -> object:
            eod_calls.append(kwargs)
            symbols = kwargs["symbols"]
            assert isinstance(symbols, tuple)
            return SimpleNamespace(
                items=tuple(_eod(symbol, eod_date, Decimal("101")) for symbol in symbols)
            )

        async def get_daily_bars(self, **kwargs: object) -> object:
            bar_calls.append(kwargs)
            symbol = kwargs["symbol"]
            assert isinstance(symbol, str)
            if symbol == "HG1":
                raise RuntimeError("provider down")
            if symbol == "XAG/USD":
                # The provider settled a different close than the 1day series.
                return SimpleNamespace(
                    items=(
                        _bar(symbol, date(2026, 9, 3), Decimal("100")),
                        _bar(symbol, eod_date, Decimal("101.5")),
                    )
                )
            return SimpleNamespace(
                items=(
                    _bar(symbol, date(2026, 9, 3), Decimal("100")),
                    _bar(symbol, eod_date, Decimal("101")),
                    # Session in progress: its close drifts with the live price.
                    _bar(symbol, date(2026, 9, 7), Decimal("120")),
                )
            )

    monkeypatch.setattr(macro, "TwelveDataTransport", _FakeTransport)
    monkeypatch.setattr(macro, "TwelveDataAdapter", FakeAdapter)
    histories = await macro.load_commodity_histories(Settings(twelve_data_api_key=SecretStr("key")))
    assert [item.symbol for item in histories] == [
        "XBR/USD",
        "WTI/USD",
        "XAU/USD",
        "XAG/USD",
        "HG1",
    ]
    assert len(eod_calls) == 1
    assert eod_calls[0]["symbols"] == ("XBR/USD", "WTI/USD", "XAU/USD", "XAG/USD", "HG1")
    assert eod_calls[0]["expected_currencies"] == dict.fromkeys(eod_calls[0]["symbols"], "USD")
    assert all(call["symbol_type"] == "commodity" for call in bar_calls)
    assert all(call["expected_currency"] == "USD" for call in bar_calls)
    assert all(call["dp"] == 11 for call in bar_calls)
    assert all(call["outputsize"] == macro.COMMODITY_HISTORY for call in bar_calls)
    assert [item.status for item in histories] == ["ok", "ok", "ok", "unavailable", "unavailable"]
    assert all(item.source == "Twelve Data" for item in histories)
    # The mutable 2026-09-07 bar is dropped; the last point is the settled EOD.
    assert [point.date for point in histories[0].points] == [date(2026, 9, 3), eod_date]
    assert histories[0].points[-1].value == Decimal("101")


async def test_commodities_degrade_together_when_eod_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdapter:
        def __init__(self, transport: object) -> None:
            pass

        async def get_eods(self, **kwargs: object) -> object:
            raise RuntimeError("provider down")

        async def get_daily_bars(self, **kwargs: object) -> object:
            raise AssertionError("no history should be requested without an EOD date")

    monkeypatch.setattr(macro, "TwelveDataTransport", _FakeTransport)
    monkeypatch.setattr(macro, "TwelveDataAdapter", FakeAdapter)
    histories = await macro.load_commodity_histories(Settings(twelve_data_api_key=SecretStr("key")))
    assert [item.status for item in histories] == ["unavailable"] * len(macro.COMMODITIES)
    assert all(item.points == [] for item in histories)


async def test_cache_coalesces_concurrent_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    markets = AsyncMock(return_value=[])
    treasury = AsyncMock(return_value=[])
    sofr = AsyncMock(
        return_value=macro.History(
            id="sofr", symbol="SOFR", unit="percent", source="New York Fed", status="unavailable"
        )
    )
    monkeypatch.setattr(macro, "load_market_histories", markets)
    monkeypatch.setattr(macro, "load_treasury", treasury)
    monkeypatch.setattr(macro, "load_sofr", sofr)
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


async def test_dashboard_reads_persisted_snapshot_without_live_provider_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from typing import cast

    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from daily_insights_api.modules.identity.api import AuthContext
    from daily_insights_api.modules.reports import router
    from daily_insights_api.modules.reports.macro_dashboard_models import MacroDashboardSnapshot

    payload = macro.MacroDashboard(
        fetched_at=datetime(2026, 9, 8, tzinfo=UTC),
        histories=[],
        calendar=macro.Calendar(status="disabled", date=date(2026, 9, 8), source=""),
    ).model_dump(mode="json")
    database = SimpleNamespace(
        get=AsyncMock(
            return_value=MacroDashboardSnapshot(
                scope_key="global_macro_bonds",
                fetched_at=datetime(2026, 9, 8, tzinfo=UTC),
                edition_date=date(2026, 9, 8),
                payload=payload,
            )
        )
    )
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace())})
    provider = AsyncMock()
    monkeypatch.setattr(
        router, "visible_report_market_codes", AsyncMock(return_value={"global_macro_bonds"})
    )
    monkeypatch.setattr(macro, "refresh_macro_dashboard", provider)

    result = await router.get_macro_dashboard(
        request, cast(AuthContext, None), cast(AsyncSession, database)
    )

    assert result.model_dump(mode="json") == payload
    provider.assert_not_awaited()
