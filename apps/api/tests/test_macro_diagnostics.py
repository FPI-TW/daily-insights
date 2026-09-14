from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.data_management import service
from daily_insights_api.modules.data_sources.errors import DataSourceTransientError
from daily_insights_api.modules.reports import macro_dashboard as macro
from daily_insights_api.modules.reports.macro_diagnostics import (
    SourceFailure,
    classify,
    diagnostics,
)


@pytest.mark.parametrize(
    "status,kind", [(403, "authentication_error"), (429, "rate_limited"), (503, "http_error")]
)
async def test_twelve_data_transport_keeps_status(status: int, kind: str) -> None:
    from pydantic import SecretStr

    from daily_insights_api.modules.data_sources.api import RetryPolicy, TwelveDataTransport
    from daily_insights_api.modules.data_sources.errors import DataSourceError

    async with httpx.AsyncClient(
        base_url="https://example.com",
        transport=httpx.MockTransport(lambda request: httpx.Response(status)),
    ) as client:
        async with TwelveDataTransport(
            base_url="https://example.com",
            api_key=SecretStr("private"),
            retry_policy=RetryPolicy(max_attempts=1),
            client=client,
        ) as transport:
            with pytest.raises(DataSourceError) as caught:
                await transport.get("/eod", params={})
    assert classify(caught.value) == (kind, status)


@pytest.mark.parametrize(
    "body,kind", [(b"not json", "parse_error"), (b'{"data": {}}', "validation_error")]
)
async def test_calendar_invalid_envelope(body: bytes, kind: str) -> None:
    entries: list[tuple[str, SourceFailure]] = []
    token = diagnostics.set(entries)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        ) as client:
            result = await macro.load_calendar(client, datetime(2026, 9, 14, tzinfo=UTC))
        assert result.status == "unavailable"
        assert {failure.failure_type for _, failure in entries} == {kind}
    finally:
        diagnostics.reset(token)


async def test_treasury_keeps_successful_year_and_reports_failed_year() -> None:
    from daily_insights_api.modules.reports.macro_diagnostics import summarize

    entries: list[tuple[str, SourceFailure]] = []
    token = diagnostics.set(entries)
    try:

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.params["field_tdr_date_value"] == "2025":
                return httpx.Response(503)
            return httpx.Response(
                200,
                content=(
                    b"<feed><properties><NEW_DATE>2026-09-11</NEW_DATE>"
                    b"<BC_10YEAR>4.0</BC_10YEAR></properties></feed>"
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            histories = await macro.load_treasury(client, datetime(2026, 9, 14).date())
        assert next(h for h in histories if h.id == "10y").status == "ok"
        source = summarize(
            entries, [("us_treasury", "U.S. Treasury", [(h.symbol, h.status) for h in histories])]
        )[0]
        assert source.status == "degraded"
        assert "BC_2YEAR" in source.affected_items
        assert any(f.http_status == 503 and f.affected_items == ["2025"] for f in source.failures)
    finally:
        diagnostics.reset(token)


@pytest.mark.parametrize(
    "rows,expected,count",
    [
        ([{"gmt": "All Day", "country": "Russia", "eventName": "Holiday"}], "ok", 0),
        ([], "ok", 0),
        ([{"gmt": "25:99", "country": "Japan", "eventName": "Invalid"}], "unavailable", 0),
        (
            [
                {"gmt": "All Day", "country": "Russia", "eventName": "Holiday"},
                {"gmt": "01:00", "country": "Japan", "eventName": "Valid"},
                {"gmt": "broken"},
            ],
            "ok",
            1,
        ),
    ],
)
async def test_calendar_isolates_rows(
    rows: list[dict[str, str]], expected: str, count: int
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": {"rows": rows}})
        )
    ) as client:
        result = await macro.load_calendar(client, datetime(2026, 9, 14, tzinfo=UTC))
    assert result.status == expected
    assert len(result.events) == count


async def test_calendar_keeps_errors_without_raw_response() -> None:
    entries: list[tuple[str, SourceFailure]] = []
    token = diagnostics.set(entries)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(429, text="secret credential")
            )
        ) as client:
            result = await macro.load_calendar(client, datetime(2026, 9, 14, tzinfo=UTC))
        assert result.status == "unavailable"
        assert len(entries) == 2
        assert all(f.failure_type == "rate_limited" and f.http_status == 429 for _, f in entries)
        assert "secret" not in str(entries)
    finally:
        diagnostics.reset(token)


def test_typed_provider_error_preserves_http_status() -> None:
    assert classify(DataSourceTransientError("private", http_status=503)) == ("http_error", 503)
    assert classify(httpx.ReadTimeout("private")) == ("timeout", None)


@pytest.mark.parametrize(
    "calendar_status,history_status,error",
    [
        ("disabled", "ok", None),
        ("disabled", "disabled", None),
        ("unavailable", "ok", "macro_calendar_unavailable"),
        ("unavailable", "unavailable", "macro_sources_unavailable"),
        ("ok", "unavailable", "macro_sources_unavailable"),
    ],
)
async def test_run_error_precedence(
    monkeypatch: pytest.MonkeyPatch, calendar_status: str, history_status: str, error: str | None
) -> None:
    dashboard = macro.MacroDashboard.model_validate(
        {
            "fetched_at": "2026-09-14T00:00:00Z",
            "histories": [
                {
                    "id": "sofr",
                    "symbol": "SOFR",
                    "unit": "percent",
                    "source": "New York Fed",
                    "status": history_status,
                }
            ],
            "calendar": {"date": "2026-09-14", "source": "Nasdaq", "status": calendar_status},
        }
    )
    monkeypatch.setattr(service, "refresh_macro_dashboard", AsyncMock(return_value=dashboard))
    status, result, actual, _ = await service._execute_macro(Settings())
    assert actual == error
    assert status == ("partial" if error else "succeeded")
    assert "sources" in result


async def test_disabled_calendar_and_multiple_source_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def markets(settings: Settings) -> list[macro.History]:
        return [
            macro.History(
                id="dxy",
                symbol="DX-Y.NYB",
                source="Yahoo Finance",
                unit="index",
                status="unavailable",
            ),
            macro.History(
                id="gold", symbol="XAU/USD", source="Twelve Data", unit="USD", status="ok"
            ),
        ]

    monkeypatch.setattr(macro, "load_market_histories", markets)
    monkeypatch.setattr(macro, "load_treasury", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        macro,
        "load_sofr",
        AsyncMock(
            return_value=macro.History(
                id="sofr",
                symbol="SOFR",
                source="New York Fed",
                unit="percent",
                status="unavailable",
            )
        ),
    )
    calendar = AsyncMock(side_effect=AssertionError("disabled calendar made a request"))
    monkeypatch.setattr(macro, "load_calendar", calendar)
    result = await macro.refresh_macro_dashboard(Settings())
    calendar.assert_not_awaited()
    sources = {source.code: source for source in result._sources}
    assert sources["nasdaq_calendar"].status == "disabled"
    assert sources["yahoo_finance"].affected_items == ["DX-Y.NYB"]
    assert sources["new_york_fed"].affected_items == ["SOFR"]
    assert "sources" not in result.model_dump_json()
    assert diagnostics.get() is None
