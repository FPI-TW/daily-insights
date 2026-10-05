import asyncio
import hashlib
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
from daily_insights_api.modules.data_sources.twelve_data.adapter import (
    TWELVE_DATA_CONTRACT_HASH,
    CompletedPricesResult,
    TwelveDataAdapter,
)
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


@pytest.mark.parametrize("symbol,asset_type", [("AAPL", "Common Stock"), ("TLT", "ETF")])
async def test_daily_bars_accept_equity_currency_field(symbol: str, asset_type: str) -> None:
    payload = {
        "meta": {
            "symbol": symbol,
            "interval": "1day",
            "currency": "USD",
            "type": asset_type,
        },
        "values": [
            {
                "datetime": "2026-09-16",
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
        market="us_equity",
        symbol=symbol,
        expected_currency="USD",
        outputsize=1,
    )

    assert result.items[0].symbol == symbol


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
    time_series_params: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        endpoints.append(request.url.path)
        if request.url.path == "/eod":
            return httpx.Response(200, json=_eod_payload("AAPL"), request=request)
        time_series_params.update(request.url.params)
        close = "100.12345678901" if request.url.params.get("dp") == "11" else "100.12346"
        return httpx.Response(200, json=_series_payload("AAPL", close), request=request)

    result = await TwelveDataAdapter(transport(httpx.MockTransport(respond))).get_completed_prices(
        market="us_equity",
        symbols=("AAPL",),
        expected_currencies={"AAPL": "USD"},
        outputsize=400,
    )

    assert endpoints == ["/eod", "/time_series"]
    assert time_series_params["dp"] == "11"
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


def _bar(
    trade_date: str,
    close: str,
    *,
    open_: str | None = None,
    high: str | None = None,
    low: str | None = None,
    volume: int | None = None,
) -> dict[str, object]:
    price = Decimal(close)
    return {
        "datetime": trade_date,
        "open": open_ or close,
        "high": high or str(price + 1),
        "low": low or str(price - 1),
        "close": close,
        "volume": volume,
    }


def _completed_adapter(
    values: list[dict[str, object]],
    *,
    symbol: str = "XAU/USD",
    eod_date: str = "2026-10-04",
    eod_close: str = "4137.51110",
    currency: str = "US Dollar",
    asset_type: str = "Precious Metal",
    requests: list[httpx.Request] | None = None,
) -> TwelveDataAdapter:
    def respond(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        payload: dict[str, object]
        if request.url.path == "/eod":
            payload = {
                "symbol": symbol,
                "exchange": "COMMODITY",
                "datetime": eod_date,
                "close": eod_close,
            }
        else:
            payload = {
                "meta": {
                    "symbol": symbol,
                    "interval": "1day",
                    "currency_quote": currency,
                    "type": asset_type,
                },
                "values": values,
                "status": "ok",
            }
        return httpx.Response(200, json=payload, request=request)

    return TwelveDataAdapter(transport(httpx.MockTransport(respond)))


async def _completed_result(
    adapter: TwelveDataAdapter,
    *,
    symbol: str = "XAU/USD",
    outputsize: int = 2,
    asset_type: str = "Precious Metal",
) -> CompletedPricesResult:
    return await adapter.get_completed_prices(
        market="global_macro_bonds",
        symbols=(symbol,),
        expected_currencies={symbol: "USD"},
        expected_asset_types={symbol: asset_type},
        symbol_types={symbol: "commodity"} if asset_type == "Precious Metal" else None,
        outputsize=outputsize,
    )


@pytest.mark.parametrize(
    "symbol,official_close,other_close,previous_close",
    [
        ("XAU/USD", "4137.51110", "4137.63144", "4130.12000"),
        ("XAG/USD", "60.36723", "60.36886", "60.00000"),
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
async def test_completed_prices_choose_unique_full_eod_row_independent_of_duplicate_order(
    symbol: str, official_close: str, other_close: str, previous_close: str, reverse: bool
) -> None:
    official = _bar("2026-10-04", official_close, volume=17)
    other = _bar("2026-10-04", other_close, volume=23)
    candidates = [official, other] if reverse else [other, official]
    result = await _completed_result(
        _completed_adapter(
            [_bar("2026-10-03", previous_close), *candidates],
            symbol=symbol,
            eod_close=official_close,
        ),
        symbol=symbol,
    )
    item = result.items[0]
    assert item.as_of == date(2026, 10, 4)
    assert item.close == Decimal(official_close)
    assert item.previous_close == Decimal(previous_close)
    assert item.bars[-1].open == Decimal(str(official["open"]))
    assert item.bars[-1].high == Decimal(str(official["high"]))
    assert item.bars[-1].low == Decimal(str(official["low"]))
    assert item.bars[-1].volume == 17
    assert len(item.bars) == 2


async def test_completed_fx_excludes_valid_duplicate_rows_beyond_official_eod() -> None:
    values = [
        _bar("2026-10-02", "1.17"),
        _bar("2026-10-03", "1.18"),
        _bar("2026-10-04", "1.19"),
        _bar("2026-10-04", "1.20"),
    ]
    result = await _completed_result(
        _completed_adapter(
            values,
            symbol="EUR/USD",
            eod_date="2026-10-03",
            eod_close="1.18",
            asset_type="Physical Currency",
        ),
        symbol="EUR/USD",
        asset_type="Physical Currency",
    )
    item = result.items[0]
    assert [bar.trade_date for bar in item.bars] == [date(2026, 10, 2), date(2026, 10, 3)]
    assert item.previous_close == Decimal("1.17")
    assert item.provenances[-1].record_count == 2
    assert item.provenances[-1].as_of == date(2026, 10, 3)


async def test_completed_prices_collapse_consumed_value_identical_rows_only() -> None:
    previous = _bar("2026-10-03", "4130")
    official = _bar("2026-10-04", "4137.51110")
    equivalent = {**official, "close": "4137.51110000", "unused_provider_field": "ignored"}
    result = await _completed_result(
        _completed_adapter([previous, dict(previous), official, equivalent])
    )
    assert len(result.items[0].bars) == 2
    assert result.items[0].bars[-1].volume is None


@pytest.mark.parametrize(
    "change",
    [{"open": "4137"}, {"high": "4140"}, {"low": "4130"}, {"volume": 1}, {"volume": 0}],
)
async def test_completed_prices_reject_distinct_eod_rows_with_same_official_close(
    change: dict[str, object],
) -> None:
    official = _bar("2026-10-04", "4137.51110")
    with pytest.raises(DataSourceContractError, match="one distinct"):
        await _completed_result(
            _completed_adapter([_bar("2026-10-03", "4130"), official, {**official, **change}])
        )


async def test_completed_prices_reject_eod_conflict_without_matching_close() -> None:
    with pytest.raises(DataSourceContractError, match="one distinct"):
        await _completed_result(
            _completed_adapter(
                [
                    _bar("2026-10-03", "4130"),
                    _bar("2026-10-04", "4137"),
                    _bar("2026-10-04", "4138"),
                ]
            )
        )


@pytest.mark.parametrize("field,value", [("close", "4131"), ("volume", 0)])
async def test_completed_prices_reject_conflicting_historical_rows(
    field: str, value: object
) -> None:
    previous = _bar("2026-10-03", "4130")
    with pytest.raises(DataSourceContractError, match=r"historical.*conflict"):
        await _completed_result(
            _completed_adapter(
                [previous, {**previous, field: value}, _bar("2026-10-04", "4137.51110")]
            )
        )


@pytest.mark.parametrize(
    "dates",
    [
        ["2026-10-04", "2026-10-03"],
        ["2026-10-03", "2026-10-04", "2026-10-06", "2026-10-05"],
        ["2026-10-03", "2026-10-05", "2026-10-04"],
    ],
)
async def test_completed_prices_reject_raw_descending_dates_even_after_cutoff(
    dates: list[str],
) -> None:
    with pytest.raises(DataSourceContractError, match="strictly ascending"):
        await _completed_result(_completed_adapter([_bar(day, "4137.51110") for day in dates]))


@pytest.mark.parametrize("change", [{"high": "4136"}, {"low": "4139"}, {"open": "4140"}])
async def test_completed_prices_validate_nonmatching_candidate_before_selection(
    change: dict[str, object],
) -> None:
    nonmatching = {**_bar("2026-10-04", "4137.63144"), **change}
    with pytest.raises(DataSourceContractError, match="OHLC range"):
        await _completed_result(
            _completed_adapter(
                [_bar("2026-10-03", "4130"), nonmatching, _bar("2026-10-04", "4137.51110")]
            )
        )


@pytest.mark.parametrize("day", ["2026-10-04", "2026-10-05"])
@pytest.mark.parametrize(
    "change",
    [{"open": None}, {"close": "NaN"}, {"high": "Infinity"}, {"volume": -1}],
)
async def test_completed_prices_reject_schema_invalid_current_or_future_candidate(
    day: str, change: dict[str, object]
) -> None:
    with pytest.raises(DataSourceContractError, match="reviewed contract"):
        await _completed_result(
            _completed_adapter(
                [
                    _bar("2026-10-03", "4130"),
                    _bar("2026-10-04", "4137.51110"),
                    {**_bar(day, "4138"), **change},
                ]
            )
        )


async def test_completed_prices_count_distinct_completed_sessions_for_history() -> None:
    official = _bar("2026-10-04", "4137.51110")
    with pytest.raises(DataSourceContractError, match="required history"):
        await _completed_result(
            _completed_adapter([official, dict(official), _bar("2026-10-05", "4138")])
        )


async def test_completed_prices_reject_latest_date_that_is_not_official_eod() -> None:
    with pytest.raises(DataSourceContractError, match="did not match"):
        await _completed_result(
            _completed_adapter([_bar("2026-10-02", "4120"), _bar("2026-10-03", "4130")])
        )


@pytest.mark.parametrize("currency,asset_type", [("Euro", "Precious Metal"), ("US Dollar", "ETF")])
async def test_completed_prices_preserve_metadata_validation(
    currency: str,
    asset_type: str,
) -> None:
    with pytest.raises(DataSourceContractError, match="launch manifest"):
        await _completed_result(
            _completed_adapter(
                [_bar("2026-10-03", "4130"), _bar("2026-10-04", "4137.51110")],
                currency=currency,
                asset_type=asset_type,
            )
        )


@pytest.mark.parametrize("outputsize,requested", [(1, 4), (2, 4), (400, 400), (5000, 5000)])
async def test_completed_prices_provenance_records_raw_response_and_actual_query(
    outputsize: int, requested: int
) -> None:
    requests: list[httpx.Request] = []
    result = await _completed_result(
        _completed_adapter(
            [
                _bar("2026-10-03", "4130"),
                _bar("2026-10-04", "4137.63144"),
                _bar("2026-10-04", "4137.51110"),
                _bar("2026-10-05", "4138"),
            ],
            requests=requests,
        ),
        outputsize=outputsize,
    )
    request = requests[-1]
    params = {
        "symbol": "XAU/USD",
        "interval": "1day",
        "outputsize": requested,
        "order": "ASC",
        "type": "commodity",
        "dp": 11,
    }
    assert {key: value for key, value in request.url.params.items() if key != "apikey"} == {
        key: str(value) for key, value in params.items()
    }
    provenance = result.items[0].provenances[-1]
    assert (
        provenance.query_fingerprint
        == hashlib.sha256(
            json.dumps(sorted(params.items()), separators=(",", ":")).encode()
        ).hexdigest()
    )
    # Rebuild the identical mock response bytes, including rejected/future rows.
    payload = {
        "meta": {
            "symbol": "XAU/USD",
            "interval": "1day",
            "currency_quote": "US Dollar",
            "type": "Precious Metal",
        },
        "values": [
            _bar("2026-10-03", "4130"),
            _bar("2026-10-04", "4137.63144"),
            _bar("2026-10-04", "4137.51110"),
            _bar("2026-10-05", "4138"),
        ],
        "status": "ok",
    }
    assert (
        provenance.response_digest
        == hashlib.sha256(httpx.Response(200, json=payload).content).hexdigest()
    )
    assert provenance.record_count == 2
    assert provenance.as_of == date(2026, 10, 4)
    assert provenance.contract_version == "2026-10-05.v8"
    assert provenance.contract_hash == TWELVE_DATA_CONTRACT_HASH
    assert (
        provenance.contract_hash
        != hashlib.sha256(
            b"twelve-data:eod,time_series,completed-daily-bars,dp11:2026-09-17.v7"
        ).hexdigest()
    )


async def test_completed_prices_enforce_provider_outputsize_upper_bound() -> None:
    with pytest.raises(ValueError, match="outputsize"):
        await _completed_result(_completed_adapter([]), outputsize=5001)


async def test_generic_daily_bars_still_reject_identical_duplicate_dates() -> None:
    official = _bar("2026-10-04", "4137.51110")
    with pytest.raises(DataSourceContractError, match="strictly ascending"):
        await _completed_adapter([official, dict(official)]).get_daily_bars(
            market="global_macro_bonds", symbol="XAU/USD", expected_currency="USD", outputsize=2
        )


async def test_completed_prices_do_not_impose_universal_price_positivity() -> None:
    result = await _completed_result(
        _completed_adapter([_bar("2026-10-03", "-1"), _bar("2026-10-04", "4137.51110")])
    )
    assert result.items[0].previous_close == Decimal("-1")
