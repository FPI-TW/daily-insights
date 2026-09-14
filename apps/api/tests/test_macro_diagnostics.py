from datetime import datetime
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


def test_typed_provider_error_preserves_http_status() -> None:
    assert classify(DataSourceTransientError("private", http_status=503)) == ("http_error", 503)
    assert classify(httpx.ReadTimeout("private")) == ("timeout", None)


@pytest.mark.parametrize(
    "history_status,error",
    [
        ("ok", None),
        ("disabled", None),
        ("unavailable", "macro_sources_unavailable"),
    ],
)
async def test_run_error_precedence(
    monkeypatch: pytest.MonkeyPatch, history_status: str, error: str | None
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
            "calendar": {"date": "2026-09-14", "source": "", "status": "disabled"},
        }
    )
    monkeypatch.setattr(service, "refresh_macro_dashboard", AsyncMock(return_value=dashboard))
    status, result, actual, _ = await service._execute_macro(Settings())
    assert actual == error
    assert status == ("partial" if error else "succeeded")
    assert "sources" in result


async def test_multiple_source_failures_without_calendar_source(
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
    result = await macro.refresh_macro_dashboard(Settings())
    sources = {source.code: source for source in result._sources}
    assert set(sources) == {"twelve_data", "yahoo_finance", "us_treasury", "new_york_fed"}
    assert sources["yahoo_finance"].affected_items == ["DX-Y.NYB"]
    assert sources["new_york_fed"].affected_items == ["SOFR"]
    assert "sources" not in result.model_dump_json()
    assert diagnostics.get() is None
