"""Current macro context, separate from immutable morning-report publications.

Public market histories are shared across viewers for five minutes, but the
router checks organization access before consulting the cache. Failed sources
remain explicit and never borrow a value from another instrument.
"""

import asyncio
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from defusedxml.ElementTree import fromstring
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.data_sources.api import YfinanceAdapter


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
    events: list[EconomicEvent] = Field(default_factory=list)


class MacroDashboard(BaseModel):
    fetched_at: datetime
    histories: list[History]
    calendar: Calendar


# Explicit quote directions and units. Commodity symbols are front-month
# futures, not the Twelve Data spot closes in the published morning report.
INSTRUMENTS = (
    ("brent", "BZ=F", "USD/bbl"),
    ("wti", "CL=F", "USD/bbl"),
    ("gold", "GC=F", "USD/oz"),
    ("silver", "SI=F", "USD/oz"),
    ("copper", "HG=F", "USD/lb"),
    ("dxy", "DX-Y.NYB", "index"),
    ("eur_usd", "EURUSD=X", "USD"),
    ("gbp_usd", "GBPUSD=X", "USD"),
    ("aud_usd", "AUDUSD=X", "USD"),
    ("nzd_usd", "NZDUSD=X", "USD"),
    ("usd_jpy", "JPY=X", "JPY"),
    ("usd_chf", "CHF=X", "CHF"),
    ("usd_cad", "CAD=X", "CAD"),
    ("usd_twd", "TWD=X", "TWD"),
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
CALENDAR_URL = "https://financialmodelingprep.com/stable/economic-calendar"


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


async def load_treasury(client: httpx.AsyncClient, today: date) -> list[History]:
    async def fetch(year: int) -> bytes:
        response = await client.get(
            TREASURY_URL,
            params={
                "data": "daily_treasury_yield_curve",
                "field_tdr_date_value": year,
            },
        )
        response.raise_for_status()
        return response.content

    try:
        # A third year covers the previous observation when the one-year
        # comparison falls on a New Year holiday.
        payloads = await asyncio.gather(
            *(fetch(year) for year in range(today.year - 2, today.year + 1)),
            return_exceptions=True,
        )
        return treasury_histories(
            [payload for payload in payloads if isinstance(payload, bytes)], today
        )
    except Exception:
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
            raise ValueError("SOFR history is empty")
        return History(
            id="sofr",
            symbol="SOFR",
            unit="percent",
            source="New York Fed",
            status="ok",
            points=[Point(date=day, value=value) for day, value in sorted(values.items())],
        )
    except Exception:
        return History(
            id="sofr", symbol="SOFR", unit="percent", source="New York Fed", status="unavailable"
        )


async def load_calendar(client: httpx.AsyncClient, settings: Settings, now: datetime) -> Calendar:
    today = now.astimezone(ZoneInfo("Asia/Taipei")).date()
    if settings.fmp_api_key is None or not settings.fmp_api_key.get_secret_value().strip():
        return Calendar(status="disabled", date=today)
    try:
        response = await client.get(
            CALENDAR_URL,
            params={
                "from": (today - timedelta(days=1)).isoformat(),
                "to": today.isoformat(),
                "apikey": settings.fmp_api_key.get_secret_value(),
            },
        )
        response.raise_for_status()
        events = TypeAdapter(list[EconomicEvent]).validate_python(response.json())
        selected = []
        for event in events:
            # FMP calendar timestamps are UTC even when the offset is omitted.
            moment = event.date.replace(tzinfo=UTC) if event.date.tzinfo is None else event.date
            if moment.astimezone(ZoneInfo("Asia/Taipei")).date() == today:
                # Do not reveal a provider's premature actual for a future event.
                selected.append(
                    event.model_copy(
                        update={
                            "date": moment,
                            "actual": event.actual if moment <= now else None,
                        }
                    )
                )
        return Calendar(
            status="ok", date=today, events=sorted(selected, key=lambda item: item.date)
        )
    except Exception:
        return Calendar(status="unavailable", date=today)


async def load_market_histories(settings: Settings) -> list[History]:
    if not settings.yfinance_enabled:
        return [
            History(id=key, symbol=symbol, unit=unit, source="Yahoo Finance", status="disabled")
            for key, symbol, unit in INSTRUMENTS
        ]
    adapter = YfinanceAdapter(timeout_seconds=min(settings.yfinance_timeout_seconds, 10))
    semaphore = asyncio.Semaphore(4)

    async def fetch(key: str, symbol: str, unit: str) -> History:
        async with semaphore:
            try:
                result = await adapter.get_daily_bars(market="global_macro_bonds", symbol=symbol)
                points = [
                    Point(date=item.trade_date, value=item.close)
                    for item in result.items
                    if item.close is not None
                ]
                if not points or any(point.value <= 0 for point in points):
                    raise ValueError("market history must contain positive closes")
                return History(
                    id=key,
                    symbol=symbol,
                    unit=unit,
                    source="Yahoo Finance",
                    status="ok",
                    points=points,
                )
            except Exception:
                return History(
                    id=key, symbol=symbol, unit=unit, source="Yahoo Finance", status="unavailable"
                )

    return list(await asyncio.gather(*(fetch(*instrument) for instrument in INSTRUMENTS)))


class MacroDashboardService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._cached: MacroDashboard | None = None
        self._expires = 0.0

    async def get(self) -> MacroDashboard:
        async with self._lock:
            now = datetime.now(UTC)
            today = now.astimezone(ZoneInfo("Asia/Taipei")).date()
            if (
                self._cached is not None
                and time.monotonic() < self._expires
                and self._cached.calendar.date == today
            ):
                return self._cached
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                markets, treasury, sofr, calendar = await asyncio.gather(
                    load_market_histories(self.settings),
                    load_treasury(client, today),
                    load_sofr(client, today),
                    load_calendar(client, self.settings, now),
                )
            result = MacroDashboard(
                fetched_at=datetime.now(UTC),
                histories=[*markets, *treasury, sofr],
                calendar=calendar,
            )
            self._cached = result
            self._expires = time.monotonic() + 300
            return result
