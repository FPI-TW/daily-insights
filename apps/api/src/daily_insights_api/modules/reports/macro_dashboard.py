"""Current macro context, separate from immutable morning-report publications.

Public market histories are shared across viewers for five minutes, but the
router checks organization access before consulting the cache. Failed sources
remain explicit and never borrow a value from another instrument.
"""

import asyncio
import html
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from defusedxml.ElementTree import fromstring
from pydantic import BaseModel, ConfigDict, Field

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
    source: str
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
CALENDAR_URL = "https://api.nasdaq.com/api/calendar/economicevents"
CALENDAR_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
    "User-Agent": "Mozilla/5.0",
}
CALENDAR_SOURCE = "Nasdaq"
CALENDAR_VALUE = re.compile(
    r"^\s*(?P<prefix>[$€£¥])?\s*"
    r"(?P<number>[+-]?(?:\d+(?:,\d{3})*|\d+)(?:\.\d+)?)\s*"
    r"(?P<suffix>%|[KMBT]|bps?)?\s*$",
    re.IGNORECASE,
)
COUNTRY_DETAILS = {
    "Argentina": ("AR", "ARS"),
    "Australia": ("AU", "AUD"),
    "Brazil": ("BR", "BRL"),
    "Canada": ("CA", "CAD"),
    "Chile": ("CL", "CLP"),
    "China": ("CN", "CNY"),
    "Colombia": ("CO", "COP"),
    "Czech Republic": ("CZ", "CZK"),
    "Denmark": ("DK", "DKK"),
    "Euro Area": ("EU", "EUR"),
    "European Union": ("EU", "EUR"),
    "France": ("FR", "EUR"),
    "Germany": ("DE", "EUR"),
    "Hong Kong": ("HK", "HKD"),
    "Hungary": ("HU", "HUF"),
    "India": ("IN", "INR"),
    "Indonesia": ("ID", "IDR"),
    "Ireland": ("IE", "EUR"),
    "Israel": ("IL", "ILS"),
    "Italy": ("IT", "EUR"),
    "Japan": ("JP", "JPY"),
    "Mexico": ("MX", "MXN"),
    "New Zealand": ("NZ", "NZD"),
    "Norway": ("NO", "NOK"),
    "Poland": ("PL", "PLN"),
    "Portugal": ("PT", "EUR"),
    "Saudi Arabia": ("SA", "SAR"),
    "Singapore": ("SG", "SGD"),
    "South Africa": ("ZA", "ZAR"),
    "South Korea": ("KR", "KRW"),
    "Spain": ("ES", "EUR"),
    "Switzerland": ("CH", "CHF"),
    "Sweden": ("SE", "SEK"),
    "Taiwan": ("TW", "TWD"),
    "Turkey": ("TR", "TRY"),
    "United Kingdom": ("GB", "GBP"),
    "United States": ("US", "USD"),
}


class NasdaqCalendarRow(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gmt: str = Field(pattern=r"^\d{2}:\d{2}$")
    country: str = Field(min_length=1, max_length=100)
    event_name: str = Field(alias="eventName", min_length=1, max_length=300)
    actual: str | int | float | None = None
    consensus: str | int | float | None = None
    previous: str | int | float | None = None


class NasdaqCalendarData(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rows: list[NasdaqCalendarRow] | None = None


class NasdaqCalendarResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data: NasdaqCalendarData | None = None


def parse_calendar_value(raw: str | int | float | None) -> tuple[Decimal | None, str | None]:
    if raw is None:
        return None, None
    text = html.unescape(str(raw)).replace("\xa0", " ").strip()
    if not text:
        return None, None
    match = CALENDAR_VALUE.fullmatch(text)
    if match is None:
        return None, None
    prefix = match.group("prefix")
    suffix = match.group("suffix")
    prefix_unit = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}.get(prefix)
    unit = suffix.upper() if suffix else prefix_unit
    if prefix_unit and suffix:
        unit = f"{prefix_unit} {suffix.upper()}"
    return Decimal(match.group("number").replace(",", "")), unit


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


async def load_calendar(client: httpx.AsyncClient, now: datetime) -> Calendar:
    today = now.astimezone(ZoneInfo("Asia/Taipei")).date()
    try:
        query_days = (today - timedelta(days=1), today)
        responses = await asyncio.gather(
            *(
                client.get(
                    CALENDAR_URL,
                    params={"date": day.isoformat()},
                    headers=CALENDAR_HEADERS,
                )
                for day in query_days
            ),
            return_exceptions=True,
        )
        selected: list[EconomicEvent] = []
        successful_response = False
        for query_day, response in zip(query_days, responses, strict=True):
            if not isinstance(response, httpx.Response):
                continue
            try:
                response.raise_for_status()
                payload = NasdaqCalendarResponse.model_validate(response.json())
            except Exception:
                continue
            successful_response = True
            for row in payload.data.rows if payload.data and payload.data.rows else []:
                moment = datetime.combine(
                    query_day,
                    datetime.strptime(row.gmt, "%H:%M").time(),
                    tzinfo=UTC,
                )
                if moment.astimezone(ZoneInfo("Asia/Taipei")).date() != today:
                    continue
                actual, actual_unit = parse_calendar_value(row.actual)
                estimate, estimate_unit = parse_calendar_value(row.consensus)
                previous, previous_unit = parse_calendar_value(row.previous)
                country_code, currency = COUNTRY_DETAILS.get(row.country, (row.country, None))
                selected.append(
                    EconomicEvent(
                        date=moment,
                        country=country_code,
                        event=row.event_name,
                        currency=currency,
                        estimate=estimate,
                        previous=previous,
                        actual=actual if moment <= now else None,
                        unit=actual_unit or estimate_unit or previous_unit,
                    )
                )
        if not successful_response:
            return Calendar(status="unavailable", date=today, source=CALENDAR_SOURCE)
        return Calendar(
            status="ok",
            date=today,
            source=CALENDAR_SOURCE,
            events=sorted(selected, key=lambda item: (item.date, item.country, item.event)),
        )
    except Exception:
        return Calendar(status="unavailable", date=today, source=CALENDAR_SOURCE)


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
                    load_calendar(client, now),
                )
            result = MacroDashboard(
                fetched_at=datetime.now(UTC),
                histories=[*markets, *treasury, sofr],
                calendar=calendar,
            )
            self._cached = result
            self._expires = time.monotonic() + 300
            return result
