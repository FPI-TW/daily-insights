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


async def test_stock_movers_accept_provider_market_local_datetime() -> None:
    payload = {
        "values": [
            {
                "symbol": "ACME",
                "name": "Acme Corp",
                "exchange": "NASDAQ",
                "mic_code": "XNAS",
                "datetime": "2026-08-28 15:59:00",
                "last": 12.5,
                "high": 13.0,
                "low": 10.0,
                "volume": 100,
                "change": 2.5,
                "percent_change": 25.0,
            }
        ],
        "status": "ok",
    }
    adapter = TwelveDataAdapter(
        transport(
            httpx.MockTransport(lambda request: httpx.Response(200, json=payload, request=request))
        )
    )

    result = await adapter.get_stock_movers(direction="gainers", outputsize=1)

    assert result.items[0].as_of == date(2026, 8, 28)
    assert result.items[0].close == Decimal("12.5")
    assert result.provenance.record_count == 1


async def test_stock_movers_reject_datetime_with_utc_offset() -> None:
    payload = {
        "values": [
            {
                "symbol": "ACME",
                "name": "Acme Corp",
                "exchange": "NASDAQ",
                "mic_code": "XNAS",
                "datetime": "2026-08-28T15:59:00-04:00",
                "last": 12.5,
                "high": 13.0,
                "low": 10.0,
                "volume": 100,
                "change": 2.5,
                "percent_change": 25.0,
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
        await adapter.get_stock_movers(direction="gainers", outputsize=1)


async def test_stock_movers_reject_datetime_without_zero_padding() -> None:
    payload = {
        "values": [
            {
                "symbol": "ACME",
                "name": "Acme Corp",
                "exchange": "NASDAQ",
                "mic_code": "XNAS",
                "datetime": "2026-8-2 3:4:5",
                "last": 12.5,
                "high": 13.0,
                "low": 10.0,
                "volume": 100,
                "change": 2.5,
                "percent_change": 25.0,
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
        await adapter.get_stock_movers(direction="gainers", outputsize=1)


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


@pytest.mark.parametrize("field", ["percent_change", "timestamp"])
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
