"""Macro snapshot models, instrument catalog, and orchestration rate loaders.

Orchestration publishes the durable snapshot read by HTTP handlers. Treasury
and SOFR loaders retain explicit failures for orchestration diagnostics.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

import httpx
from defusedxml.ElementTree import fromstring
from pydantic import BaseModel, ConfigDict, Field

from daily_insights_api.modules.reports.macro_diagnostics import (
    EmptySourceResponse,
    record_failure,
)


class Point(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    date: date
    value: Decimal


class History(BaseModel):
    id: str
    symbol: str
    unit: str
    source: str
    status: Literal["ok", "unavailable", "disabled"]
    points: list[Point] = Field(default_factory=list)
    base_dates: dict[str, date] = Field(default_factory=dict)


class EconomicEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    date: datetime
    country: str = Field(max_length=100)
    event: str = Field(min_length=1, max_length=300)
    currency: str | None = None
    impact: str | None = None
    estimate: Decimal | None = None
    previous: Decimal | None = None
    actual: Decimal | None = None
    unit: str | None = None


class Calendar(BaseModel):
    status: Literal["ok", "unavailable", "disabled"]
    date: date
    source: str
    events: list[EconomicEvent] = Field(default_factory=list)


class MacroDashboard(BaseModel):
    fetched_at: datetime
    histories: list[History]
    calendar: Calendar


# Shared catalog used by orchestration projections and news analysis.
COMMODITIES = (
    ("brent", "XBR/USD", "USD/bbl", "Energy Resource"),
    ("wti", "WTI/USD", "USD/bbl", "Energy Resource"),
    ("gold", "XAU/USD", "USD/oz", "Precious Metal"),
    ("silver", "XAG/USD", "USD/oz", "Precious Metal"),
    ("copper", "HG1", "USD/lb", "Industrial Metal"),
)
# DXY remains on Yahoo Finance because it is an index rather than a currency
# pair. All FX pairs below use Twelve Data's physical-currency daily series.
INSTRUMENTS = (("dxy", "DX-Y.NYB", "index"),)
FX_INSTRUMENTS = (
    ("eur_usd", "EUR/USD", "USD"),
    ("gbp_usd", "GBP/USD", "USD"),
    ("aud_usd", "AUD/USD", "USD"),
    ("nzd_usd", "NZD/USD", "USD"),
    ("usd_jpy", "USD/JPY", "JPY"),
    ("usd_chf", "USD/CHF", "CHF"),
    ("usd_cad", "USD/CAD", "CAD"),
    ("usd_twd", "USD/TWD", "TWD"),
    ("usd_krw", "USD/KRW", "KRW"),
    ("usd_hkd", "USD/HKD", "HKD"),
    ("usd_cnh", "USD/CNH", "CNH"),
    ("usd_sgd", "USD/SGD", "SGD"),
    ("eur_jpy", "EUR/JPY", "JPY"),
    ("aud_jpy", "AUD/JPY", "JPY"),
)
TENORS = (
    ("3m", "BC_3MONTH"),
    ("2y", "BC_2YEAR"),
    ("5y", "BC_5YEAR"),
    ("10y", "BC_10YEAR"),
    ("30y", "BC_30YEAR"),
)
TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
)
SOFR_URL = "https://markets.newyorkfed.org/api/rates/secured/sofr/search.json"


def treasury_histories(payloads: list[bytes], today: date) -> list[History]:
    values: dict[str, dict[date, Decimal]] = {tenor: {} for tenor, _ in TENORS}
    for payload in payloads:
        root = fromstring(payload)
        for row in root.iter():
            if row.tag.rsplit("}", 1)[-1] != "properties":
                continue
            fields = {child.tag.rsplit("}", 1)[-1]: child.text for child in row}
            raw_date = fields.get("NEW_DATE")
            if not raw_date:
                continue
            day = date.fromisoformat(raw_date[:10])
            if day > today:
                continue
            for tenor, field in TENORS:
                raw = fields.get(field)
                if raw is not None:
                    point = Point(date=day, value=raw)
                    values[tenor][day] = point.value
    return [
        History(
            id=tenor,
            symbol=field,
            unit="percent",
            source="U.S. Treasury",
            status="ok" if values[tenor] else "unavailable",
            points=[Point(date=day, value=value) for day, value in sorted(values[tenor].items())],
        )
        for tenor, field in TENORS
    ]


class SofrRate(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    effectiveDate: date
    percentRate: Decimal
    type: Literal["SOFR"]


class SofrResponse(BaseModel):
    refRates: list[SofrRate]


async def load_treasury(
    client: httpx.AsyncClient,
    today: date,
    *,
    years: tuple[int, ...] | None = None,
    months: tuple[tuple[int, int], ...] = (),
) -> list[History]:
    async def fetch(period_type: str, period: int) -> bytes:
        period_parameter = (
            "field_tdr_date_value" if period_type == "year" else "field_tdr_date_value_month"
        )
        response = await client.get(
            TREASURY_URL,
            params={
                "data": "daily_treasury_yield_curve",
                period_parameter: period,
            },
        )
        response.raise_for_status()
        return response.content

    try:
        # A third year covers the previous observation when the one-year
        # comparison falls on a New Year holiday.
        # Treasury intermittently stalls when several XML feeds are opened
        # concurrently from the same client/IP. Fetch the selected year/month
        # periods sequentially so each failure remains independently diagnosable.
        requested_periods = (
            tuple(("year", year, str(year)) for year in range(today.year - 2, today.year + 1))
            if years is None
            else (
                *tuple(("year", year, str(year)) for year in sorted(set(years))),
                *tuple(
                    ("month", year * 100 + month, f"{year:04d}-{month:02d}")
                    for year, month in sorted(set(months))
                ),
            )
        )
        payloads: list[bytes | Exception] = []
        for period_type, period, _ in requested_periods:
            try:
                payloads.append(await fetch(period_type, period))
            except Exception as error:
                payloads.append(error)
        valid = []
        for (_, _, label), payload in zip(requested_periods, payloads, strict=True):
            if isinstance(payload, Exception):
                record_failure("us_treasury", "yield_curve_xml", [label], payload)
                continue
            try:
                fromstring(payload)
                valid.append(payload)
            except Exception as error:
                record_failure("us_treasury", "yield_curve_xml", [label], error)
        histories = treasury_histories(valid, today)
        for history in histories:
            if history.status != "ok":
                record_failure("us_treasury", "yield_curve_xml", [history.symbol])
        return histories
    except Exception as error:
        record_failure("us_treasury", "yield_curve_xml", [field for _, field in TENORS], error)
        return [
            History(
                id=tenor, symbol=field, unit="percent", source="U.S. Treasury", status="unavailable"
            )
            for tenor, field in TENORS
        ]


async def load_sofr(client: httpx.AsyncClient, today: date) -> History:
    try:
        response = await client.get(
            SOFR_URL,
            params={
                "startDate": (today - timedelta(days=740)).isoformat(),
                "endDate": today.isoformat(),
            },
        )
        response.raise_for_status()
        parsed = SofrResponse.model_validate(response.json())
        values = {
            item.effectiveDate: item.percentRate
            for item in parsed.refRates
            if item.effectiveDate <= today
        }
        if not values:
            raise EmptySourceResponse("SOFR history is empty")
        return History(
            id="sofr",
            symbol="SOFR",
            unit="percent",
            source="New York Fed",
            status="ok",
            points=[Point(date=day, value=value) for day, value in sorted(values.items())],
        )
    except Exception as error:
        record_failure("new_york_fed", "sofr/search.json", ["SOFR"], error)
        return History(
            id="sofr", symbol="SOFR", unit="percent", source="New York Fed", status="unavailable"
        )
