import asyncio
import json
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


async def test_quote_is_normalized_to_decimal_and_sanitized_provenance() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "apikey super-secret-key"
        assert "super-secret-key" not in str(request.url)
        return httpx.Response(
            200,
            headers={"x-request-id": "td-123", "api-credits-used": "1"},
            json={
                "symbol": "XAU/USD",
                "name": "Gold Spot",
                "currency": "USD",
                "datetime": "2026-08-29",
                "timestamp": 1788033600,
                "open": "2410.10",
                "high": "2421.00",
                "low": "2409.90",
                "close": "2418.25",
                "previous_close": "2410.10",
                "change": "8.15",
                "percent_change": "0.3382",
            },
        )

    result = await TwelveDataAdapter(transport(httpx.MockTransport(respond))).get_quote(
        market="global_macro_bonds", symbol="XAU/USD", expected_currency="USD"
    )
    assert result.close == Decimal("2418.25")
    assert result.as_of == date(2026, 8, 29)
    assert result.provenance.provider == "twelve_data"
    assert result.provenance.request_id == "td-123"
    assert "super-secret-key" not in json.dumps(result.provenance.model_dump(mode="json"))


async def test_quote_uses_explicit_symbol_quote_currency_when_provider_omits_currency() -> None:
    payload = {
        "symbol": "XAU/USD",
        "datetime": "2026-08-29",
        "timestamp": 1788033600,
        "open": "1",
        "high": "2",
        "low": "1",
        "close": "2",
        "previous_close": "1",
        "percent_change": "1",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    result = await adapter.get_quote(
        market="global_macro_bonds", symbol="XAU/USD", expected_currency="USD"
    )

    assert result.currency == "USD"
    assert result.as_of == date(2026, 8, 29)


async def test_quote_timestamp_is_normalized_at_utc_date_boundary() -> None:
    payload = {
        "symbol": "XAU/USD",
        "datetime": "2026-08-30",
        "timestamp": 1788049800,
        "open": "1",
        "high": "2",
        "low": "1",
        "close": "2",
        "previous_close": "1",
        "percent_change": "1",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    result = await adapter.get_quote(
        market="global_macro_bonds", symbol="XAU/USD", expected_currency="USD"
    )

    assert result.as_of == date(2026, 8, 30)


async def test_quote_rejects_non_epoch_timestamp() -> None:
    payload = {
        "symbol": "XAU/USD",
        "datetime": "2026-08-29",
        "timestamp": "2026-08-29T00:30:00-05:00",
        "open": "1",
        "high": "2",
        "low": "1",
        "close": "2",
        "previous_close": "1",
        "percent_change": "1",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    with pytest.raises(DataSourceContractError):
        await adapter.get_quote(
            market="global_macro_bonds", symbol="XAU/USD", expected_currency="USD"
        )


async def test_quote_rejects_datetime_outside_observed_calendar_date_format() -> None:
    payload = {
        "symbol": "XAU/USD",
        "datetime": "2026-8-2",
        "timestamp": 1788049800,
        "open": "1",
        "high": "2",
        "low": "1",
        "close": "2",
        "previous_close": "1",
        "percent_change": "1",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    with pytest.raises(DataSourceContractError):
        await adapter.get_quote(
            market="global_macro_bonds", symbol="XAU/USD", expected_currency="USD"
        )


@pytest.mark.parametrize("status", [401, 403])
async def test_authentication_failures_are_not_retried(status: int) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, request=request)

    with pytest.raises(DataSourceAuthenticationError):
        await transport(httpx.MockTransport(respond)).get("/quote", params={"symbol": "BTC/USD"})
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
        "/quote", params={"symbol": "BTC/USD"}
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
        await transport(httpx.MockTransport(respond)).get("/quote", params={"symbol": "BTC/USD"})
    assert calls == 3


async def test_timeout_has_bounded_attempts() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("provider timed out", request=request)

    with pytest.raises(DataSourceTransientError):
        await transport(httpx.MockTransport(respond)).get("/quote", params={"symbol": "BTC/USD"})
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
        *(bounded.get("/quote", params={"symbol": str(index)}) for index in range(5))
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
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    accepted = await adapter.get_daily_bars(
        market="global_macro_bonds",
        symbol="XBR/USD",
        expected_currency="USD",
        expected_asset_type="Energy Resource",
        outputsize=1,
    )
    assert accepted.items[0].symbol == "XBR/USD"

    with pytest.raises(DataSourceContractError, match="asset type"):
        await adapter.get_daily_bars(
            market="global_macro_bonds",
            symbol="XBR/USD",
            expected_currency="USD",
            expected_asset_type="Precious Metal",
            outputsize=1,
        )


@pytest.mark.parametrize("currency_quote", [None, "Euro"])
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


@pytest.mark.parametrize("field", ["previous_close", "timestamp"])
async def test_quote_rejects_missing_manifest_required_field(field: str) -> None:
    payload: dict[str, object] = {
        "symbol": "XAU/USD",
        "currency": "USD",
        "datetime": "2026-08-29",
        "timestamp": 1788033600,
        "open": "1",
        "high": "2",
        "low": "1",
        "close": "2",
        "previous_close": "1",
        "percent_change": "1",
    }
    payload[field] = None
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    with pytest.raises(DataSourceContractError):
        await adapter.get_quote(
            market="global_macro_bonds", symbol="XAU/USD", expected_currency="USD"
        )


async def test_quote_rejects_non_iso_currency_unit() -> None:
    payload = {
        "symbol": "XAU/USD",
        "currency": "US Dollars",
        "datetime": "2026-08-29",
        "timestamp": 1788033600,
        "open": "1",
        "high": "2",
        "low": "1",
        "close": "2",
        "previous_close": "1",
        "percent_change": "1",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    with pytest.raises(DataSourceContractError, match="invalid currency unit"):
        await adapter.get_quote(
            market="global_macro_bonds", symbol="XAU/USD", expected_currency="USD"
        )


async def test_quote_rejects_currency_that_differs_from_manifest_unit() -> None:
    payload = {
        "symbol": "HG1",
        "currency": "USD",
        "datetime": "2026-08-28",
        "timestamp": 1787958000,
        "open": "1",
        "high": "2",
        "low": "1",
        "close": "2",
        "previous_close": "1",
        "percent_change": "1",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    with pytest.raises(DataSourceContractError, match="launch manifest"):
        await adapter.get_quote(market="global_macro_bonds", symbol="HG1", expected_currency="EUR")


@pytest.mark.parametrize("close", [None, "NaN", "not-a-decimal"])
async def test_invalid_or_missing_decimal_is_a_contract_error(close: object) -> None:
    payload = {
        "symbol": "BTC/USD",
        "datetime": "2026-08-29",
        "timestamp": 1788033600,
        "open": "1",
        "high": "1",
        "low": "1",
        "close": close,
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )
    with pytest.raises(DataSourceContractError):
        await adapter.get_quote(market="crypto", symbol="BTC/USD", expected_currency="USD")


def _quote_payload(
    symbol: str, close: str, previous_close: str, **extra: object
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "currency": "USD",
        "datetime": "2026-09-02",
        "timestamp": 1788393600,
        "open": "1",
        "high": "2",
        "low": "1",
        "close": close,
        "previous_close": previous_close,
        "percent_change": "0",
        **extra,
    }


async def test_batch_quotes_keep_request_order_and_split_commodity_types() -> None:
    requests: list[dict[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requests.append(params)
        if params.get("type") == "commodity":
            # Commodity quotes carry no currency field.
            return httpx.Response(
                200,
                json=_quote_payload("HG1", "6.49132", "6.51794", currency=None, name="Copper Spot"),
            )
        return httpx.Response(
            200,
            json={
                "XAU/USD": _quote_payload("XAU/USD", "4427.74", "4387.77", currency=None),
                "XBR/USD": _quote_payload("XBR/USD", "93.996", "94.33", currency=None),
            },
        )

    adapter = TwelveDataAdapter(transport(httpx.MockTransport(respond)))
    result = await adapter.get_quotes(
        market="global_macro_bonds",
        symbols=("XBR/USD", "XAU/USD", "HG1"),
        expected_currencies={"XBR/USD": "USD", "XAU/USD": "USD", "HG1": "USD"},
        symbol_types={"HG1": "commodity"},
    )

    assert [item.symbol for item in result.items] == ["XBR/USD", "XAU/USD", "HG1"]
    assert [item.currency for item in result.items] == ["USD", "USD", "USD"]
    assert result.items[2].previous_close == Decimal("6.51794")
    assert requests == [
        {"symbol": "XBR/USD,XAU/USD"},
        {"symbol": "HG1", "type": "commodity"},
    ]
    assert len(result.provenances) == 2
    assert result.provenances[0].record_count == 2


async def test_batch_quotes_reject_a_per_symbol_error_object_and_missing_symbols() -> None:
    def respond_with_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "SPY": _quote_payload("SPY", "765.16", "761.78"),
                "VIX": {"code": 404, "message": "symbol invalid", "status": "error"},
            },
        )

    adapter = TwelveDataAdapter(transport(httpx.MockTransport(respond_with_error)))
    with pytest.raises(DataSourceContractError, match="reviewed contract"):
        await adapter.get_quotes(
            market="us_equity",
            symbols=("SPY", "VIX"),
            expected_currencies={"SPY": "USD", "VIX": "USD"},
        )

    def respond_short(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"SPY": _quote_payload("SPY", "765.16", "761.78")})

    adapter = TwelveDataAdapter(transport(httpx.MockTransport(respond_short)))
    with pytest.raises(DataSourceContractError, match="every symbol"):
        await adapter.get_quotes(
            market="us_equity",
            symbols=("SPY", "QQQ"),
            expected_currencies={"SPY": "USD", "QQQ": "USD"},
        )


async def test_quote_rejects_zero_previous_close() -> None:
    payload = _quote_payload("AAPL", "324.96", "0")
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    with pytest.raises(DataSourceContractError, match="previous close"):
        await adapter.get_quote(market="us_equity", symbol="AAPL", expected_currency="USD")


async def test_non_commodity_quote_without_currency_is_still_rejected() -> None:
    payload = _quote_payload("AAPL", "324.96", "325.13", currency=None)
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    with pytest.raises(DataSourceContractError, match="currency unit"):
        await adapter.get_quote(market="us_equity", symbol="AAPL", expected_currency="USD")
