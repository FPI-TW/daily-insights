"""Current macro context, separate from immutable morning-report publications.

Public market histories are shared across viewers for five minutes, but the
router checks organization access before consulting the cache. Failed sources
remain explicit and never borrow a value from another instrument.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from defusedxml.ElementTree import fromstring
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.data_sources.api import (
    EodResult,
    RetryPolicy,
    TwelveDataAdapter,
    TwelveDataTransport,
    YfinanceAdapter,
)
from daily_insights_api.modules.reports.macro_diagnostics import (
    EmptySourceResponse,
    SourceDiagnostic,
    SourceFailure,
    diagnostics,
    record_failure,
    summarize,
)
from daily_insights_api.modules.reports.morning_report import completed_history_for_eod


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
    _sources: list[SourceDiagnostic] = PrivateAttr(default_factory=list)
    fetched_at: datetime
    histories: list[History]
    calendar: Calendar


# Commodities use the same Twelve Data spot symbols and the same /eod cut as
# the published morning report, so the dashboard and the report never disagree
# on a close: the 1day series still carries the session in progress, whose
# close moves with the live price until the provider settles it, so the history
# is cut at the /eod date and its last close must match /eod exactly. Without
# type=commodity the provider resolves HG1 to a Frankfurt-listed stock.
COMMODITY_SOURCE = "Twelve Data"
COMMODITIES = (
    ("brent", "XBR/USD", "USD/bbl", "Energy Resource"),
    ("wti", "WTI/USD", "USD/bbl", "Energy Resource"),
    ("gold", "XAU/USD", "USD/oz", "Precious Metal"),
    ("silver", "XAG/USD", "USD/oz", "Precious Metal"),
    ("copper", "HG1", "USD/lb", "Industrial Metal"),
)
# Enough sessions to cover the one-year ratio window with margin.
COMMODITY_HISTORY = 400
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
FX_TIMEZONE = ZoneInfo("Australia/Sydney")
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
        # Treasury intermittently stalls every request when several yearly XML
        # feeds are opened concurrently from the same client/IP. Fetching the
        # three bounded years sequentially is both faster in production and
        # keeps each year's failure independently diagnosable.
        payloads: list[bytes | Exception] = []
        for year in range(today.year - 2, today.year + 1):
            try:
                payloads.append(await fetch(year))
            except Exception as error:
                payloads.append(error)
        valid = []
        for year, payload in zip(range(today.year - 2, today.year + 1), payloads, strict=True):
            if isinstance(payload, Exception):
                record_failure("us_treasury", "yield_curve_xml", [str(year)], payload)
                continue
            try:
                fromstring(payload)
                valid.append(payload)
            except Exception as error:
                record_failure("us_treasury", "yield_curve_xml", [str(year)], error)
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


async def load_commodity_histories(settings: Settings) -> list[History]:
    def fallback(status: Literal["unavailable", "disabled"]) -> list[History]:
        return [
            History(id=key, symbol=symbol, unit=unit, source=COMMODITY_SOURCE, status=status)
            for key, symbol, unit, _asset_type in COMMODITIES
        ]

    if settings.twelve_data_api_key is None:
        return fallback("disabled")

    async def fetch(
        adapter: TwelveDataAdapter,
        eod: EodResult,
        key: str,
        symbol: str,
        unit: str,
        asset_type: str,
    ) -> History:
        try:
            result = await adapter.get_daily_bars(
                market="global_macro_bonds",
                symbol=symbol,
                expected_currency="USD",
                expected_asset_type=asset_type,
                symbol_type="commodity",
                # The last close is compared exactly with /eod, so both endpoints
                # must use the same reviewed provider precision.
                dp=11,
                outputsize=COMMODITY_HISTORY,
            )
            completed = completed_history_for_eod(result.items, eod)
            points = [
                Point(date=item.trade_date, value=item.close)
                for item in completed
                if item.close is not None
            ]
            if not points:
                raise EmptySourceResponse("commodity history is empty")
            if any(point.value <= 0 for point in points):
                raise ValueError("market history must contain positive closes")
            return History(
                id=key,
                symbol=symbol,
                unit=unit,
                source=COMMODITY_SOURCE,
                status="ok",
                points=points,
            )
        except Exception as error:
            record_failure("twelve_data", "time_series", [symbol], error)
            return History(
                id=key, symbol=symbol, unit=unit, source=COMMODITY_SOURCE, status="unavailable"
            )

    try:
        async with TwelveDataTransport(
            base_url=settings.twelve_data_base_url,
            api_key=settings.twelve_data_api_key,
            timeout_seconds=min(settings.twelve_data_timeout_seconds, 10),
            retry_policy=RetryPolicy(max_attempts=settings.twelve_data_retry_attempts),
            max_concurrency=settings.twelve_data_max_concurrency,
        ) as transport:
            adapter = TwelveDataAdapter(transport)
            # One batch request settles every commodity's completed date; without
            # it no history can be cut, so a failure here degrades all five.
            eods = await adapter.get_eods(
                market="global_macro_bonds",
                symbols=tuple(symbol for _key, symbol, _unit, _asset_type in COMMODITIES),
                expected_currencies={
                    symbol: "USD" for _key, symbol, _unit, _asset_type in COMMODITIES
                },
            )
            return list(
                await asyncio.gather(
                    *(
                        fetch(adapter, eod, *commodity)
                        for eod, commodity in zip(eods.items, COMMODITIES, strict=True)
                    )
                )
            )
    except Exception as error:
        record_failure("twelve_data", "eod", [item[1] for item in COMMODITIES], error)
        return fallback("unavailable")


async def load_market_histories(settings: Settings) -> list[History]:
    commodities, dxy, fx = await asyncio.gather(
        load_commodity_histories(settings), load_dxy_history(settings), load_fx_histories(settings)
    )
    return [*commodities, *dxy, *fx]


async def load_dxy_history(settings: Settings) -> list[History]:
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
                if not points:
                    raise EmptySourceResponse("FX history is empty")
                if any(point.value <= 0 for point in points):
                    raise ValueError("market history must contain positive closes")
                return History(
                    id=key,
                    symbol=symbol,
                    unit=unit,
                    source="Yahoo Finance",
                    status="ok",
                    points=points,
                )
            except Exception as error:
                record_failure("yahoo_finance", "history", [symbol], error)
                return History(
                    id=key, symbol=symbol, unit=unit, source="Yahoo Finance", status="unavailable"
                )

    return list(await asyncio.gather(*(fetch(*instrument) for instrument in INSTRUMENTS)))


def fx_base_dates(points: list[Point]) -> dict[str, date]:
    if not points:
        return {}
    latest = points[-1].date
    return {
        str(days): next(
            point.date for point in points if point.date >= latest - timedelta(days=days - 1)
        )
        for days in (30, 90, 365)
    }


def fx_provider_end_date(now: datetime) -> date:
    """Return Twelve Data's exclusive FX cutoff in its requested timezone."""
    return now.astimezone(FX_TIMEZONE).date()


async def load_fx_histories(settings: Settings) -> list[History]:
    if settings.twelve_data_api_key is None:
        return [
            History(id=key, symbol=symbol, unit=unit, source="Twelve Data", status="disabled")
            for key, symbol, unit in FX_INSTRUMENTS
        ]

    async def fetch(adapter: TwelveDataAdapter, key: str, symbol: str, unit: str) -> History:
        try:
            # Twelve Data's end_date is exclusive. Requesting the same timezone
            # used to derive it keeps the still-forming provider day out.
            result = await adapter.get_daily_bars(
                market="global_macro_bonds",
                symbol=symbol,
                expected_currency=unit,
                outputsize=400,
                end_date=fx_provider_end_date(datetime.now(UTC)),
                timezone="Australia/Sydney",
            )
            points = [
                Point(date=item.trade_date, value=item.close)
                for item in result.items
                if item.close is not None
            ]
            if not points:
                raise EmptySourceResponse("FX history is empty")
            if any(point.value <= 0 for point in points):
                raise ValueError("market history must contain positive closes")
            return History(
                id=key,
                symbol=symbol,
                unit=unit,
                source="Twelve Data",
                status="ok",
                points=points,
                base_dates=fx_base_dates(points),
            )
        except Exception as error:
            record_failure("twelve_data", "forex_history", [symbol], error)
            return History(
                id=key, symbol=symbol, unit=unit, source="Twelve Data", status="unavailable"
            )

    try:
        async with TwelveDataTransport(
            base_url=settings.twelve_data_base_url,
            api_key=settings.twelve_data_api_key,
            timeout_seconds=min(settings.twelve_data_timeout_seconds, 10),
            retry_policy=RetryPolicy(max_attempts=settings.twelve_data_retry_attempts),
            max_concurrency=settings.twelve_data_max_concurrency,
        ) as transport:
            adapter = TwelveDataAdapter(transport)
            return list(
                await asyncio.gather(
                    *(fetch(adapter, *instrument) for instrument in FX_INSTRUMENTS)
                )
            )
    except Exception as error:
        record_failure("twelve_data", "forex_history", [item[1] for item in FX_INSTRUMENTS], error)
        return [
            History(id=key, symbol=symbol, unit=unit, source="Twelve Data", status="unavailable")
            for key, symbol, unit in FX_INSTRUMENTS
        ]


async def refresh_macro_dashboard(settings: Settings) -> MacroDashboard:
    """Fetch a complete dashboard payload for durable queue publication.

    This is deliberately not used by an HTTP handler.  A dashboard request is
    served only from the last transactionally published snapshot.
    """
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo("Asia/Taipei")).date()
    entries: list[tuple[str, SourceFailure]] = []
    token = diagnostics.set(entries)
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            markets, treasury, sofr = await asyncio.gather(
                load_market_histories(settings),
                load_treasury(client, today),
                load_sofr(client, today),
            )
            calendar = Calendar(status="disabled", date=today, source="")
        dashboard = MacroDashboard(
            fetched_at=datetime.now(UTC), histories=[*markets, *treasury, sofr], calendar=calendar
        )
        states = [
            (code, name, [(h.symbol, h.status) for h in dashboard.histories if h.source == name])
            for code, name in (
                ("twelve_data", COMMODITY_SOURCE),
                ("yahoo_finance", "Yahoo Finance"),
                ("us_treasury", "U.S. Treasury"),
                ("new_york_fed", "New York Fed"),
            )
        ]
        dashboard._sources = summarize(entries, states, dashboard.fetched_at)
        return dashboard
    finally:
        diagnostics.reset(token)


class MacroDashboardService:
    """Compatibility wrapper for non-HTTP callers.

    The application no longer registers this service: production refreshes are
    invoked by the durable data-management worker and HTTP always reads DB.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()
        self._cached: MacroDashboard | None = None

    async def get(self) -> MacroDashboard:
        async with self._lock:
            if self._cached is None:
                self._cached = await refresh_macro_dashboard(self.settings)
            return self._cached
