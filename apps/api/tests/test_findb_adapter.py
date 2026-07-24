import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from daily_insights_api.modules.data_sources.dto import DailyBarQuery, InstrumentQuery
from daily_insights_api.modules.data_sources.errors import (
    DataSourceAuthenticationError,
    DataSourceContractError,
    DataSourceTransientError,
    UnsupportedMarketError,
)
from daily_insights_api.modules.data_sources.findb.adapter import (
    FINDB_CONTRACT_HASH,
    FINDB_CONTRACT_VERSION,
    FinDBAdapter,
)
from daily_insights_api.modules.data_sources.findb.transport import (
    FinDBTransport,
    RetryPolicy,
)

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "findb"
Handler = Callable[[httpx.Request], httpx.Response]


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIRECTORY / name).read_bytes()


def fixture_json(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(fixture_bytes(name)))


def make_transport(
    handler: Handler,
    *,
    api_key: str = "test-only-secret-key",
    retry_policy: RetryPolicy | None = None,
    delays: list[float] | None = None,
) -> tuple[FinDBTransport, httpx.AsyncClient]:
    async def record_delay(delay: float) -> None:
        if delays is not None:
            delays.append(delay)

    client = httpx.AsyncClient(
        base_url="https://findb.invalid",
        transport=httpx.MockTransport(handler),
    )
    return (
        FinDBTransport(
            base_url="https://findb.invalid",
            api_key=SecretStr(api_key),
            client=client,
            retry_policy=retry_policy,
            sleep=record_delay,
            random_value=lambda: 0,
        ),
        client,
    )


@pytest.mark.asyncio
async def test_instrument_page_maps_to_immutable_provider_dtos() -> None:
    secret = "test-only-secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == secret
        assert request.url.params["market"] == "US"
        assert request.url.params["page_size"] == "100"
        return httpx.Response(
            200,
            content=fixture_bytes("instruments-page.json"),
            headers={"X-Request-ID": " request-123 "},
        )

    transport, client = make_transport(handler, api_key=secret)
    try:
        result = await FinDBAdapter(transport).get_instruments(InstrumentQuery(market="us_equity"))
    finally:
        await client.aclose()

    assert result.items[0].source_id == "00000000-0000-0000-0000-000000000001"
    assert result.items[0].market == "us_equity"
    assert result.items[0].latest_price == Decimal("100.90")
    assert result.provenance.contract_version == FINDB_CONTRACT_VERSION
    assert result.provenance.contract_hash == FINDB_CONTRACT_HASH
    assert result.provenance.endpoint == "/api/v1/serve/instruments"
    assert result.provenance.as_of == date(2026, 7, 23)
    assert result.provenance.record_count == 1
    assert result.provenance.request_id == "request-123"
    assert result.provenance.fetched_at.tzinfo is UTC
    assert secret not in result.provenance.model_dump_json()
    assert "Synthetic Example" not in result.provenance.model_dump_json()

    with pytest.raises(ValidationError):
        result.items[0].symbol = "MUTATED"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_eod_page_preserves_decimal_date_and_aware_datetimes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["market"] == "US"
        assert request.url.params["symbols"] == "EXAMPLE,SECOND"
        assert request.url.params["start_date"] == "2026-07-01"
        assert request.url.params["end_date"] == "2026-07-23"
        return httpx.Response(200, content=fixture_bytes("eod-page.json"))

    transport, client = make_transport(handler)
    try:
        result = await FinDBAdapter(transport).get_daily_bars(
            DailyBarQuery(
                market="us_equity",
                symbols=("EXAMPLE", "SECOND"),
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 23),
            )
        )
    finally:
        await client.aclose()

    item = result.items[0]
    assert item.trade_date == date(2026, 7, 23)
    assert item.open == Decimal("100.10")
    assert item.close == Decimal("100.90")
    assert item.turnover == Decimal("124530.60")
    assert item.source_created_at == datetime(2026, 7, 24, tzinfo=UTC)
    assert item.source_updated_at == datetime(2026, 7, 24, 0, 5, tzinfo=UTC)
    assert result.provenance.as_of == date(2026, 7, 23)


@pytest.mark.asyncio
async def test_unknown_market_mappings_fail_closed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=fixture_bytes("instruments-page.json"))

    transport, client = make_transport(handler)
    try:
        adapter = FinDBAdapter(transport)
        with pytest.raises(UnsupportedMarketError, match="forex"):
            await adapter.get_instruments(InstrumentQuery(market="forex"))

        payload = fixture_json("instruments-page.json")
        payload["data"][0]["market"] = "HK"

        async with httpx.AsyncClient(
            base_url="https://findb.invalid",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload, request=request)
            ),
        ) as unknown_client:
            unknown_transport = FinDBTransport(
                base_url="https://findb.invalid",
                api_key=SecretStr("secret"),
                client=unknown_client,
            )
            with pytest.raises(UnsupportedMarketError, match="HK"):
                await FinDBAdapter(unknown_transport).get_instruments(InstrumentQuery())
    finally:
        await client.aclose()

    assert calls == 0


@pytest.mark.asyncio
async def test_additive_fields_are_allowed_but_missing_required_fields_fail() -> None:
    missing_success = fixture_json("instruments-page.json")
    del missing_success["success"]
    responses = iter(
        [
            fixture_bytes("instruments-page.json"),
            fixture_bytes("instruments-missing-required.json"),
            json.dumps(missing_success).encode(),
        ]
    )

    transport, client = make_transport(
        lambda request: httpx.Response(200, content=next(responses), request=request)
    )
    try:
        adapter = FinDBAdapter(transport)
        page = await adapter.get_instruments(InstrumentQuery())
        assert page.items[0].symbol == "EXAMPLE"
        with pytest.raises(DataSourceContractError, match="reviewed contract"):
            await adapter.get_instruments(InstrumentQuery())
        with pytest.raises(DataSourceContractError, match="reviewed contract"):
            await adapter.get_instruments(InstrumentQuery())
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_changed_required_type_and_naive_datetime_fail_contract() -> None:
    invalid_uuid = fixture_json("instruments-page.json")
    invalid_uuid["data"][0]["instrument_id"] = "not-a-uuid"
    naive_datetime = fixture_json("eod-page.json")
    naive_datetime["data"][0]["updated_at"] = "2026-07-24T00:05:00"
    responses = iter([invalid_uuid, naive_datetime])

    transport, client = make_transport(
        lambda request: httpx.Response(200, json=next(responses), request=request)
    )
    try:
        adapter = FinDBAdapter(transport)
        with pytest.raises(DataSourceContractError):
            await adapter.get_instruments(InstrumentQuery())
        with pytest.raises(DataSourceContractError):
            await adapter.get_daily_bars(DailyBarQuery())
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_decimal_json_number_is_rejected_as_contract_drift() -> None:
    payload = fixture_json("eod-page.json")
    payload["data"][0]["close"] = 100.9
    transport, client = make_transport(
        lambda request: httpx.Response(200, json=payload, request=request)
    )
    try:
        with pytest.raises(DataSourceContractError, match="reviewed contract"):
            await FinDBAdapter(transport).get_daily_bars(DailyBarQuery())
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_authentication_errors_are_typed_and_do_not_expose_key() -> None:
    secret = "never-log-this-key"
    transport, client = make_transport(
        lambda request: httpx.Response(
            403,
            json={"detail": "Invalid API key"},
            request=request,
        ),
        api_key=secret,
    )
    try:
        with pytest.raises(DataSourceAuthenticationError) as captured:
            await FinDBAdapter(transport).get_instruments(InstrumentQuery())
    finally:
        await client.aclose()

    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert secret not in repr(transport)


@pytest.mark.asyncio
async def test_retries_429_5xx_and_network_failures_with_a_bound() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("synthetic connection failure", request=request)
        if attempts == 2:
            return httpx.Response(429, headers={"Retry-After": "0.5"}, request=request)
        if attempts == 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=fixture_bytes("instruments-page.json"), request=request)

    transport, client = make_transport(
        handler,
        retry_policy=RetryPolicy(
            max_attempts=4,
            base_delay_seconds=0.1,
            max_delay_seconds=1,
            jitter_ratio=0,
        ),
        delays=delays,
    )
    try:
        page = await FinDBAdapter(transport).get_instruments(InstrumentQuery())
    finally:
        await client.aclose()

    assert page.items[0].symbol == "EXAMPLE"
    assert attempts == 4
    assert delays == [0.1, 0.5, 0.4]


@pytest.mark.asyncio
async def test_transient_failure_stops_after_configured_attempts() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, request=request)

    transport, client = make_transport(
        handler,
        retry_policy=RetryPolicy(
            max_attempts=3,
            base_delay_seconds=0,
            max_delay_seconds=0,
            jitter_ratio=0,
        ),
    )
    try:
        with pytest.raises(DataSourceTransientError, match="after 3 attempts"):
            await FinDBAdapter(transport).get_instruments(InstrumentQuery())
    finally:
        await client.aclose()

    assert attempts == 3


@pytest.mark.asyncio
async def test_network_error_does_not_retain_request_with_secret_header() -> None:
    secret = "network-secret-that-must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic network failure", request=request)

    transport, client = make_transport(
        handler,
        api_key=secret,
        retry_policy=RetryPolicy(
            max_attempts=1,
            base_delay_seconds=0,
            max_delay_seconds=0,
            jitter_ratio=0,
        ),
    )
    try:
        with pytest.raises(DataSourceTransientError) as captured:
            await FinDBAdapter(transport).get_instruments(InstrumentQuery())
    finally:
        await client.aclose()

    assert captured.value.__cause__ is None
    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)


@pytest.mark.asyncio
async def test_instrument_cursor_and_eod_page_iteration() -> None:
    instrument_calls: list[str | None] = []

    def instrument_handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        instrument_calls.append(cursor)
        payload = fixture_json("instruments-page.json")
        if cursor is None:
            payload["pagination"]["total_records"] = None
            payload["pagination"]["total_pages"] = None
            payload["pagination"]["next_cursor"] = "opaque-next"
        else:
            payload["pagination"]["page"] = 2
            payload["pagination"]["next_cursor"] = None
        return httpx.Response(200, json=payload, request=request)

    instrument_transport, instrument_client = make_transport(instrument_handler)
    try:
        instrument_pages = [
            page
            async for page in FinDBAdapter(instrument_transport).iter_instrument_pages(
                InstrumentQuery()
            )
        ]
    finally:
        await instrument_client.aclose()

    assert len(instrument_pages) == 2
    assert instrument_calls == [None, "opaque-next"]

    eod_calls: list[str] = []

    def eod_handler(request: httpx.Request) -> httpx.Response:
        requested_page = request.url.params["page"]
        eod_calls.append(requested_page)
        payload = fixture_json("eod-page.json")
        payload["pagination"]["page"] = int(requested_page)
        payload["pagination"]["total_records"] = 2
        payload["pagination"]["total_pages"] = 2
        return httpx.Response(200, json=payload, request=request)

    eod_transport, eod_client = make_transport(eod_handler)
    try:
        eod_pages = [
            page async for page in FinDBAdapter(eod_transport).iter_daily_bar_pages(DailyBarQuery())
        ]
    finally:
        await eod_client.aclose()

    assert len(eod_pages) == 2
    assert eod_calls == ["1", "2"]


def test_query_validation_and_retry_policy_are_bounded() -> None:
    with pytest.raises(ValidationError):
        DailyBarQuery(
            start_date=date(2026, 7, 24),
            end_date=date(2026, 7, 23),
        )
    with pytest.raises(ValueError, match="between 1 and 10"):
        RetryPolicy(max_attempts=11)
