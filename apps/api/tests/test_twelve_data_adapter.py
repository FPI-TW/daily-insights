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
                "datetime": "2026-08-29T20:00:00+00:00",
                "open": "2410.10",
                "high": "2421.00",
                "low": "2409.90",
                "close": "2418.25",
                "change": "8.15",
                "percent_change": "0.3382",
            },
        )

    result = await TwelveDataAdapter(transport(httpx.MockTransport(respond))).get_quote(
        market="global_macro_bonds", symbol="XAU/USD"
    )
    assert result.close == Decimal("2418.25")
    assert result.as_of == date(2026, 8, 29)
    assert result.provenance.provider == "twelve_data"
    assert result.provenance.request_id == "td-123"
    assert "super-secret-key" not in json.dumps(result.provenance.model_dump(mode="json"))


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


async def test_daily_bars_enforce_required_history_and_volume() -> None:
    payload = {
        "meta": {
            "symbol": "BTC/USD",
            "interval": "1day",
            "exchange_timezone": "UTC",
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
        await adapter.get_daily_bars(market="crypto", symbol="BTC/USD", outputsize=2)


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
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
            "exchange_timezone": "UTC",
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
        await adapter.get_daily_bars(market="crypto", symbol="BTC/USD", outputsize=1)


@pytest.mark.parametrize("field", ["currency", "percent_change"])
async def test_quote_rejects_missing_manifest_required_field(field: str) -> None:
    payload: dict[str, object] = {
        "symbol": "XAU/USD",
        "currency": "USD",
        "datetime": "2026-08-29T20:00:00+00:00",
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
        await adapter.get_quote(market="global_macro_bonds", symbol="XAU/USD")


async def test_quote_rejects_non_iso_currency_unit() -> None:
    payload = {
        "symbol": "XAU/USD",
        "currency": "US Dollars",
        "datetime": "2026-08-29T20:00:00+00:00",
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
        await adapter.get_quote(market="global_macro_bonds", symbol="XAU/USD")


@pytest.mark.parametrize("close", [None, "NaN", "not-a-decimal"])
async def test_invalid_or_missing_decimal_is_a_contract_error(close: object) -> None:
    payload = {
        "symbol": "BTC/USD",
        "datetime": "2026-08-29T20:00:00+00:00",
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
        await adapter.get_quote(market="crypto", symbol="BTC/USD")
