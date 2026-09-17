import asyncio
from datetime import date
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from daily_insights_api.modules.data_sources.errors import (
    DataSourceAuthenticationError,
    DataSourceContractError,
    DataSourceTransientError,
)
from daily_insights_api.modules.data_sources.twelve_data.adapter import TwelveDataAdapter
from daily_insights_api.modules.data_sources.twelve_data.transport import (
    RetryPolicy,
    TwelveDataTransport,
)


def transport(
    handler: httpx.AsyncBaseTransport,
    *,
    sleep: object | None = None,
) -> TwelveDataTransport:
    async def no_sleep(delay: float) -> None:
        if callable(sleep):
            sleep(delay)

    return TwelveDataTransport(
        base_url="https://api.twelvedata.test",
        api_key=SecretStr("super-secret-key"),
        client=httpx.AsyncClient(base_url="https://api.twelvedata.test", transport=handler),
        retry_policy=RetryPolicy(max_attempts=3, jitter_ratio=0),
        sleep=no_sleep,
    )


@pytest.mark.parametrize("status", [401, 403])
async def test_authentication_failures_are_not_retried(status: int) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, request=request)

    with pytest.raises(DataSourceAuthenticationError):
        await transport(httpx.MockTransport(respond)).get("/eod", params={"symbol": "BTC/USD"})
    assert calls == 1


async def test_rate_limit_honors_bounded_retry_after() -> None:
    calls = 0
    delays: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, headers={"Retry-After": "2"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    response = await transport(httpx.MockTransport(respond), sleep=delays.append).get(
        "/eod", params={"symbol": "BTC/USD"}
    )
    assert response.api_credits_used is None
    assert calls == 3
    assert delays == [2.0, 2.0]


async def test_transient_failure_has_bounded_attempts() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    with pytest.raises(DataSourceTransientError):
        await transport(httpx.MockTransport(respond)).get("/eod", params={"symbol": "BTC/USD"})
    assert calls == 3


async def test_timeout_has_bounded_attempts() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("provider timed out", request=request)

    with pytest.raises(DataSourceTransientError):
        await transport(httpx.MockTransport(respond)).get("/eod", params={"symbol": "BTC/USD"})
    assert calls == 3


async def test_transport_enforces_global_concurrency_limit() -> None:
    active = 0
    maximum = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={"ok": True}, request=request)

    bounded = TwelveDataTransport(
        base_url="https://api.twelvedata.test",
        api_key=SecretStr("secret"),
        client=httpx.AsyncClient(
            base_url="https://api.twelvedata.test",
            transport=httpx.MockTransport(respond),
        ),
        max_concurrency=2,
    )
    await asyncio.gather(
        *(bounded.get("/eod", params={"symbol": str(index)}) for index in range(5))
    )
    assert maximum == 2


async def test_daily_bars_enforce_required_history() -> None:
    payload = {
        "meta": {
            "symbol": "BTC/USD",
            "interval": "1day",
            "currency_base": "BTC",
            "currency_quote": "US Dollar",
        },
        "values": [
            {
                "datetime": "2026-08-29",
                "open": "1",
                "high": "2",
                "low": "1",
                "close": "2",
                "volume": None,
            }
        ],
        "status": "ok",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )
    with pytest.raises(DataSourceContractError):
        await adapter.get_daily_bars(
            market="crypto", symbol="BTC/USD", expected_currency="USD", outputsize=2
        )


async def test_daily_bars_accept_provider_crypto_shape_without_volume_or_timezone() -> None:
    payload = {
        "meta": {
            "symbol": "BTC/USD",
            "interval": "1day",
            "currency_base": "BTC",
            "currency_quote": "US Dollar",
        },
        "values": [
            {
                "datetime": "2026-08-29",
                "open": "1",
                "high": "2",
                "low": "1",
                "close": "2",
            }
        ],
        "status": "ok",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    result = await adapter.get_daily_bars(
        market="crypto", symbol="BTC/USD", expected_currency="USD", outputsize=1
    )

    assert result.items[0].volume is None
    assert result.items[0].close == Decimal("2")


async def test_daily_bars_enforce_expected_provider_asset_type() -> None:
    requests: list[dict[str, str]] = []
    payload = {
        "meta": {
            "symbol": "XBR/USD",
            "interval": "1day",
            "currency_quote": "US Dollar",
            "type": "Energy Resource",
        },
        "values": [
            {
                "datetime": "2026-08-29",
                "open": "1",
                "high": "2",
                "low": "1",
                "close": "2",
            }
        ],
        "status": "ok",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        return httpx.Response(200, json=payload, request=request)

    adapter = TwelveDataAdapter(transport(httpx.MockTransport(respond)))

    accepted = await adapter.get_daily_bars(
        market="global_macro_bonds",
        symbol="XBR/USD",
        expected_currency="USD",
        expected_asset_type="Energy Resource",
        symbol_type="commodity",
        dp=11,
        outputsize=1,
    )
    assert accepted.items[0].symbol == "XBR/USD"
    assert requests[0]["type"] == "commodity"
    assert requests[0]["dp"] == "11"

    with pytest.raises(DataSourceContractError, match="asset type"):
        await adapter.get_daily_bars(
            market="global_macro_bonds",
            symbol="XBR/USD",
            expected_currency="USD",
            expected_asset_type="Precious Metal",
            outputsize=1,
        )


async def test_commodity_daily_bars_accept_hg1_iso_usd_quote_currency() -> None:
    payload = {
        "meta": {
            "symbol": "HG1",
            "interval": "1day",
            "currency_quote": "USD",
            "type": "Industrial Metal",
        },
        "values": [
            {
                "datetime": "2026-08-29",
                "open": "1",
                "high": "2",
                "low": "1",
                "close": "2",
            }
        ],
        "status": "ok",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    result = await adapter.get_daily_bars(
        market="global_macro_bonds",
        symbol="HG1",
        expected_currency="USD",
        expected_asset_type="Industrial Metal",
        symbol_type="commodity",
        dp=11,
        outputsize=1,
    )

    assert result.items[0].symbol == "HG1"


@pytest.mark.parametrize(
    ("symbol", "expected_currency", "currency_quote"),
    [
        ("USD/JPY", "JPY", "Japanese Yen"),
        ("USD/CHF", "CHF", "Swiss Franc"),
        ("USD/CAD", "CAD", "Canadian Dollar"),
        ("USD/TWD", "TWD", "Taiwan Dollar"),
        ("USD/KRW", "KRW", "Korean Won"),
        ("USD/HKD", "HKD", "Hong Kong Dollar"),
        ("USD/CNH", "CNH", "Chinese Yuan (Offshore)"),
        ("USD/SGD", "SGD", "Singapore Dollar"),
        ("EUR/JPY", "JPY", "Japanese Yen"),
        ("AUD/JPY", "JPY", "Japanese Yen"),
    ],
)
async def test_daily_bars_accept_verified_forex_quote_currencies(
    symbol: str, expected_currency: str, currency_quote: str
) -> None:
    request_params: dict[str, str] = {}
    payload = {
        "meta": {
            "symbol": symbol,
            "interval": "1day",
            "currency_quote": currency_quote,
            "type": "Physical Currency",
        },
        "values": [{"datetime": "2026-08-29", "open": "1", "high": "2", "low": "1", "close": "2"}],
        "status": "ok",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        request_params.update(request.url.params)
        return httpx.Response(200, json=payload, request=request)

    adapter = TwelveDataAdapter(transport(httpx.MockTransport(respond)))
    result = await adapter.get_daily_bars(
        market="global_macro_bonds",
        symbol=symbol,
        expected_currency=expected_currency,
        outputsize=1,
        end_date=date(2026, 8, 30),
        timezone="Australia/Sydney",
    )

    assert result.items[0].symbol == symbol
    assert request_params["end_date"] == "2026-08-30"
    assert request_params["timezone"] == "Australia/Sydney"


@pytest.mark.parametrize("currency_quote", [None, "Euro", "US Dollars"])
async def test_daily_bars_reject_quote_currency_drift(currency_quote: object) -> None:
    payload = {
        "meta": {
            "symbol": "BTC/USD",
            "interval": "1day",
            "currency_base": "Bitcoin",
            "currency_quote": currency_quote,
        },
        "values": [
            {
                "datetime": "2026-08-29",
                "open": "1",
                "high": "2",
                "low": "1",
                "close": "2",
            }
        ],
        "status": "ok",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    with pytest.raises(DataSourceContractError, match="launch manifest"):
        await adapter.get_daily_bars(
            market="crypto", symbol="BTC/USD", expected_currency="USD", outputsize=1
        )


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
async def test_daily_bars_reject_each_missing_required_value(field: str) -> None:
    value: dict[str, object] = {
        "datetime": "2026-08-29",
        "open": "1",
        "high": "2",
        "low": "1",
        "close": "2",
        "volume": 10,
    }
    value[field] = None
    payload = {
        "meta": {
            "symbol": "BTC/USD",
            "interval": "1day",
            "currency_base": "BTC",
            "currency_quote": "US Dollar",
        },
        "values": [value],
        "status": "ok",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    with pytest.raises(DataSourceContractError):
        await adapter.get_daily_bars(
            market="crypto", symbol="BTC/USD", expected_currency="USD", outputsize=1
        )


def _eod_payload(symbol: str, close: str = "100.12345678901") -> dict[str, object]:
    return {
        "symbol": symbol,
        "exchange": "COMMODITY",
        "datetime": "2026-09-02",
        "close": close,
    }


def _series_payload(symbol: str, latest_close: str = "100.12345678901") -> dict[str, object]:
    return {
        "meta": {
            "symbol": symbol,
            "interval": "1day",
            "currency_quote": "US Dollar",
        },
        "values": [
            {
                "datetime": "2026-09-01",
                "open": "98",
                "high": "100",
                "low": "97",
                "close": "99",
            },
            {
                "datetime": "2026-09-02",
                "open": "99",
                "high": "101",
                "low": "98",
                "close": latest_close,
            },
        ],
        "status": "ok",
    }


async def test_completed_prices_use_two_sessions_and_validate_latest_eod() -> None:
    endpoints: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        endpoints.append(request.url.path)
        if request.url.path == "/eod":
            return httpx.Response(200, json=_eod_payload("AAPL"), request=request)
        return httpx.Response(200, json=_series_payload("AAPL"), request=request)

    result = await TwelveDataAdapter(transport(httpx.MockTransport(respond))).get_completed_prices(
        market="us_equity",
        symbols=("AAPL",),
        expected_currencies={"AAPL": "USD"},
        outputsize=400,
    )

    assert endpoints == ["/eod", "/time_series"]
    assert result.items[0].close == Decimal("100.12345678901")
    assert result.items[0].previous_close == Decimal("99")
    assert len(result.items[0].bars) == 2


async def test_completed_prices_reject_eod_time_series_close_mismatch() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/eod":
            return httpx.Response(200, json=_eod_payload("AAPL"), request=request)
        return httpx.Response(200, json=_series_payload("AAPL", "101"), request=request)

    adapter = TwelveDataAdapter(transport(httpx.MockTransport(respond)))
    with pytest.raises(DataSourceContractError, match="did not match"):
        await adapter.get_completed_prices(
            market="us_equity",
            symbols=("AAPL",),
            expected_currencies={"AAPL": "USD"},
        )


async def test_commodity_eod_uses_one_high_precision_batch_in_requested_order() -> None:
    requests: list[dict[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "HG1": _eod_payload("HG1", "4.12345678901"),
                "XAU/USD": _eod_payload("XAU/USD", "2400.12345678901"),
                "XBR/USD": _eod_payload("XBR/USD", "80.12345678901"),
            },
        )

    result = await TwelveDataAdapter(transport(httpx.MockTransport(respond))).get_eods(
        market="global_macro_bonds",
        symbols=("XBR/USD", "XAU/USD", "HG1"),
        expected_currencies={"XBR/USD": "USD", "XAU/USD": "USD", "HG1": "USD"},
    )

    assert [item.symbol for item in result.items] == ["XBR/USD", "XAU/USD", "HG1"]
    assert result.items[2].close == Decimal("4.12345678901")
    assert result.provenance.endpoint == "/eod"
    assert result.provenance.record_count == 3
    assert requests == [{"symbol": "XBR/USD,XAU/USD,HG1", "type": "commodity", "dp": "11"}]


@pytest.mark.parametrize(
    ("currency", "raises"),
    [(None, False), ("USD", False), ("EUR", True)],
)
async def test_commodity_eod_uses_manifest_currency_when_absent_and_rejects_drift(
    currency: str | None, raises: bool
) -> None:
    brent = _eod_payload("XBR/USD")
    gold = _eod_payload("XAU/USD")
    if currency is not None:
        brent["currency"] = currency
        gold["currency"] = currency
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(
                lambda request: httpx.Response(200, json={"XBR/USD": brent, "XAU/USD": gold})
            )
        )
    )
    if raises:
        with pytest.raises(DataSourceContractError, match="currency did not match"):
            await adapter.get_eods(
                market="global_macro_bonds",
                symbols=("XBR/USD", "XAU/USD"),
                expected_currencies={"XBR/USD": "USD", "XAU/USD": "USD"},
            )
    else:
        result = await adapter.get_eods(
            market="global_macro_bonds",
            symbols=("XBR/USD", "XAU/USD"),
            expected_currencies={"XBR/USD": "USD", "XAU/USD": "USD"},
        )
        assert [item.currency for item in result.items] == ["USD", "USD"]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"XBR/USD": _eod_payload("XBR/USD")}, "cover every symbol"),
        (
            {
                "XBR/USD": _eod_payload("XBR/USD"),
                "XAU/USD": {"code": 404, "message": "invalid", "status": "error"},
            },
            "reviewed contract",
        ),
        (
            {
                "XBR/USD": _eod_payload("XBR/USD", close="0"),
                "XAU/USD": _eod_payload("XAU/USD"),
            },
            "positive",
        ),
        (
            {
                "XBR/USD": {**_eod_payload("XBR/USD"), "datetime": "not-a-date"},
                "XAU/USD": _eod_payload("XAU/USD"),
            },
            "reviewed contract",
        ),
    ],
)
async def test_commodity_eod_rejects_incomplete_or_invalid_batch_contract(
    payload: dict[str, object], error: str
) -> None:
    adapter = TwelveDataAdapter(
        transport(httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    )
    with pytest.raises(DataSourceContractError, match=error):
        await adapter.get_eods(
            market="global_macro_bonds",
            symbols=("XBR/USD", "XAU/USD"),
            expected_currencies={"XBR/USD": "USD", "XAU/USD": "USD"},
        )
