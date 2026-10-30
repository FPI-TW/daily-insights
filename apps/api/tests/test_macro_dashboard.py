import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

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


async def test_treasury_fetches_only_requested_periods() -> None:
    requested: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        period_key = next(key for key in request.url.params if key != "data")
        requested.append((period_key, request.url.params[period_key]))
        return httpx.Response(200, content=b"<feed />", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await macro.load_treasury(
            client,
            date(2026, 9, 17),
            years=(2025,),
            months=((2026, 9),),
        )

    assert requested == [
        ("field_tdr_date_value", "2025"),
        ("field_tdr_date_value_month", "202609"),
    ]


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


async def test_dashboard_authorizes_before_reading_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from typing import cast

    from fastapi import HTTPException, Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from daily_insights_api.modules.identity.api import AuthContext
    from daily_insights_api.modules.reports import router

    database = SimpleNamespace(get=AsyncMock())
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace())})
    monkeypatch.setattr(
        router, "visible_report_market_codes", AsyncMock(return_value=frozenset({"forex"}))
    )
    with pytest.raises(HTTPException) as error:
        await router.get_macro_dashboard(
            request, cast(AuthContext, None), cast(AsyncSession, database)
        )
    assert error.value.status_code == 404
    database.get.assert_not_awaited()


async def test_dashboard_reads_persisted_snapshot(
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
    monkeypatch.setattr(
        router, "visible_report_market_codes", AsyncMock(return_value={"global_macro_bonds"})
    )

    result = await router.get_macro_dashboard(
        request, cast(AuthContext, None), cast(AsyncSession, database)
    )

    assert result.model_dump(mode="json") == payload
    database.get.assert_awaited_once_with(MacroDashboardSnapshot, "global_macro_bonds")
