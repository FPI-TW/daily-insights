from datetime import datetime

import httpx
import pytest

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
        assert any(
            source == "us_treasury"
            and failure.http_status == 503
            and failure.affected_items == ["2025"]
            for source, failure in entries
        )
    finally:
        diagnostics.reset(token)


def test_typed_provider_error_preserves_http_status() -> None:
    assert classify(DataSourceTransientError("private", http_status=503)) == ("http_error", 503)
    assert classify(httpx.ReadTimeout("private")) == ("timeout", None)
