"""TWSE rwd institutional-investor adapter: BFI82U (market, TWD) and T86 (per stock, shares).

Both endpoints take exactly one trading date; there is no range form. A date
with no trading returns HTTP 200 and a Chinese `stat` message instead of a 4xx,
so "has data" is `stat == "OK"` together with a non-empty `data` list. Row order
and row count in BFI82U changed across the years, so every value is looked up
by field name, never by position.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from daily_insights_api.modules.data_sources.errors import (
    DataSourceContractError,
    DataSourceTransientError,
)

logger = logging.getLogger(__name__)

MARKET_FLOWS_PATH = "/rwd/zh/fund/BFI82U"
STOCK_FLOWS_PATH = "/rwd/zh/fund/T86"
# TAIEX daily bars need both of these: the index report carries open/high/low/
# close with no volume, and the trading highlights carry volume with only a
# close. Both answer with a whole calendar month per request, keyed on any date
# within it, and both are the English views so the dates are Gregorian rather
# than the ROC years the zh views use.
TAIEX_INDEX_PATH = "/en/indicesReport/MI_5MINS_HIST"
TAIEX_TRADING_PATH = "/en/exchangeReport/FMTQIK"
# Neither report names the index in its rows: each path serves exactly one
# series, so the payload only identifies it in the title ("2026/09 TAIEX Total
# Index Historical Data", stable across every month probed from 2024 to 2026)
# and, for the trading report, in a column name. We store these under Yahoo's
# ^TWII ticker, so this is the only thing standing between a changed endpoint
# and quietly writing some other index into that series.
TAIEX_TITLE_MARKER = "TAIEX"
TAIEX_INDEX_FIELDS = (
    "Date",
    "Opening Index",
    "Highest Index",
    "Lowest Index",
    "Closing Index",
)
TAIEX_TRADING_FIELDS = ("Date", "Trade Volume", "TAIEX")
# `ALL` also returns ~15k warrant rows; this keeps the ~1.3k securities.
STOCK_FLOWS_SELECT_TYPE = "ALLBUT0999"
# TWSE answers both "that date had no trading" and "that date is not published
# yet" with HTTP 200 and a Chinese `stat`; neither is contract drift. A morning
# run asks for today before the ~16:00 publication and gets the second one.
NO_DATA_STAT_MARKERS = (
    "沒有符合條件",
    "大於可查詢最大日期",
    # The English views answer a month that has not happened yet with this.
    "greater than today",
)
# Each transient failure waits one more interval; six intervals is the ceiling.
MAX_BACKOFF_INTERVALS = 5

# BFI82U row label -> investor_type. The 合計 row is derivable and skipped.
MARKET_FLOW_INVESTORS: Mapping[str, str] = {
    "自營商(自行買賣)": "dealer_self",
    "自營商(避險)": "dealer_hedge",
    "投信": "trust",
    "外資及陸資(不含外資自營商)": "foreign",
    "外資自營商": "foreign_dealer",
}
MARKET_FLOW_TOTAL_LABEL = "合計"
MARKET_FLOW_FIELDS = ("單位名稱", "買進金額", "賣出金額", "買賣差額")

# T86 (buy, sell, net) column names per investor_type. The 自營商 and 三大法人
# aggregate columns are derivable and skipped.
STOCK_FLOW_COLUMNS: Mapping[str, tuple[str, str, str]] = {
    "foreign": (
        "外陸資買進股數(不含外資自營商)",
        "外陸資賣出股數(不含外資自營商)",
        "外陸資買賣超股數(不含外資自營商)",
    ),
    "foreign_dealer": ("外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數"),
    "trust": ("投信買進股數", "投信賣出股數", "投信買賣超股數"),
    "dealer_self": (
        "自營商買進股數(自行買賣)",
        "自營商賣出股數(自行買賣)",
        "自營商買賣超股數(自行買賣)",
    ),
    "dealer_hedge": ("自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)"),
}
STOCK_SYMBOL_FIELD = "證券代號"
STOCK_NAME_FIELD = "證券名稱"

# Reported alongside the numbers so a reader can tell which shape of the source
# they came from. The hash covers every field this module reads by name, so a
# renamed or dropped column changes it.
TWSE_CONTRACT_VERSION = "twse-institutional-v1"
BFI82U_ENDPOINT = MARKET_FLOWS_PATH
T86_ENDPOINT = STOCK_FLOWS_PATH
TWSE_CONTRACT_HASH = hashlib.sha256(
    json.dumps(
        {
            "BFI82U": [MARKET_FLOW_FIELDS, sorted(MARKET_FLOW_INVESTORS)],
            "T86": [STOCK_SYMBOL_FIELD, STOCK_NAME_FIELD, sorted(STOCK_FLOW_COLUMNS.values())],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
).hexdigest()

# Separate from TWSE_CONTRACT_VERSION: the institutional endpoints and the
# TAIEX ones drift independently, and a stored bar should say which shape of
# which report it came from. There is no matching hash: the institutional
# endpoints have one because their responses carry a Provenance to the API,
# and index_daily_bars stores only the version.
TAIEX_CONTRACT_VERSION = "twse-taiex-v1"

Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]


@dataclass(frozen=True, slots=True)
class TwseMarketFlow:
    investor_type: str
    buy_amount: int
    sell_amount: int
    net_amount: int


@dataclass(frozen=True, slots=True)
class TwseMarketFlows:
    trade_date: date
    # Empty means TWSE reported no trading for that date.
    items: tuple[TwseMarketFlow, ...]
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class TwseStockFlow:
    symbol: str
    security_name: str
    investor_type: str
    buy_shares: int
    sell_shares: int
    net_shares: int


@dataclass(frozen=True, slots=True)
class TwseStockFlows:
    trade_date: date
    items: tuple[TwseStockFlow, ...]
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class TaiexDailyBar:
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    # None when the index report published a session the trading highlights
    # have not; the column is nullable and a missing volume must not cost us an
    # otherwise complete bar.
    volume: int | None


@dataclass(frozen=True, slots=True)
class TaiexDailyBars:
    """One calendar month of TAIEX bars, in trade-date order."""

    month: date
    items: tuple[TaiexDailyBar, ...]
    fetched_at: datetime


def _parse_int(value: object, field: str) -> int:
    if not isinstance(value, str):
        raise DataSourceContractError(f"{field} is not a string")
    try:
        return int(value.replace(",", "").strip())
    except ValueError as error:
        raise DataSourceContractError(f"{field} is not an integer") from error


def _rows(payload: Mapping[str, Any], trade_date: date) -> list[list[Any]] | None:
    """Return the data rows, or None when TWSE has no rows for that date."""
    stat = payload.get("stat")
    if stat != "OK":
        if isinstance(stat, str) and any(marker in stat for marker in NO_DATA_STAT_MARKERS):
            return None
        logger.warning("twse returned unexpected stat %r for %s", stat, trade_date)
        raise DataSourceContractError("unexpected stat")
    reported_date = payload.get("date")
    if reported_date is not None and reported_date != trade_date.strftime("%Y%m%d"):
        raise DataSourceContractError("response date does not match request")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise DataSourceContractError("data is not a list")
    return rows or None


def _field_indexes(payload: Mapping[str, Any], required: Sequence[str]) -> dict[str, int]:
    fields = payload.get("fields")
    if not isinstance(fields, list):
        raise DataSourceContractError("fields is not a list")
    indexes = {name: index for index, name in enumerate(fields) if isinstance(name, str)}
    missing = [name for name in required if name not in indexes]
    if missing:
        raise DataSourceContractError(f"missing fields: {', '.join(missing)}")
    return indexes


def _cell(row: list[Any], index: int, field: str) -> Any:
    if index >= len(row):
        raise DataSourceContractError(f"row is missing {field}")
    return row[index]


def _check_net(buy: int, sell: int, net: int, label: str) -> None:
    if net != buy - sell:
        raise DataSourceContractError(f"{label} net does not equal buy minus sell")


def _parse_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise DataSourceContractError(f"{field} is not a string")
    try:
        return Decimal(value.replace(",", "").strip())
    except InvalidOperation as error:
        raise DataSourceContractError(f"{field} is not a number") from error


def _parse_trade_date(value: object, field: str) -> date:
    """The English views date rows as `YYYY/MM/DD`."""
    if not isinstance(value, str):
        raise DataSourceContractError(f"{field} is not a string")
    try:
        return datetime.strptime(value.strip(), "%Y/%m/%d").date()
    except ValueError as error:
        raise DataSourceContractError(f"{field} is not a YYYY/MM/DD date") from error


def _month_rows(payload: Mapping[str, Any], month: date) -> list[list[Any]] | None:
    """`_rows` for the month-wide TAIEX endpoints.

    It differs only in the date check: these echo back the requested date, which
    is any day inside the month rather than a trading date, so matching it
    against a single trade date would reject every valid response.
    """
    stat = payload.get("stat")
    if stat != "OK":
        if isinstance(stat, str) and any(marker in stat for marker in NO_DATA_STAT_MARKERS):
            return None
        logger.warning("twse returned unexpected stat %r for %s", stat, month)
        raise DataSourceContractError("unexpected stat")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise DataSourceContractError("data is not a list")
    return rows or None


def parse_taiex_index_history(
    payload: Mapping[str, Any], *, month: date
) -> dict[date, tuple[Decimal, Decimal, Decimal, Decimal]]:
    """Open/high/low/close per trade date from MI_5MINS_HIST."""
    rows = _month_rows(payload, month)
    if rows is None:
        return {}
    title = payload.get("title")
    if not isinstance(title, str) or TAIEX_TITLE_MARKER not in title:
        raise DataSourceContractError(f"index history is not a {TAIEX_TITLE_MARKER} report")
    indexes = _field_indexes(payload, TAIEX_INDEX_FIELDS)
    bars: dict[date, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    for row in rows:
        if not isinstance(row, list):
            raise DataSourceContractError("index history row is not a list")
        trade_date = _parse_trade_date(_cell(row, indexes["Date"], "Date"), "Date")
        if trade_date.year != month.year or trade_date.month != month.month:
            raise DataSourceContractError(f"{trade_date} is outside the requested month")
        if trade_date in bars:
            raise DataSourceContractError(f"index history repeated {trade_date}")
        values = [
            _parse_decimal(_cell(row, indexes[field], field), field)
            for field in ("Opening Index", "Highest Index", "Lowest Index", "Closing Index")
        ]
        bars[trade_date] = (values[0], values[1], values[2], values[3])
    return bars


def parse_taiex_trading_volumes(
    payload: Mapping[str, Any], *, month: date
) -> dict[date, tuple[int, Decimal]]:
    """Volume and the close it belongs to, per trade date, from FMTQIK."""
    rows = _month_rows(payload, month)
    if rows is None:
        return {}
    indexes = _field_indexes(payload, TAIEX_TRADING_FIELDS)
    volumes: dict[date, tuple[int, Decimal]] = {}
    for row in rows:
        if not isinstance(row, list):
            raise DataSourceContractError("trading highlights row is not a list")
        trade_date = _parse_trade_date(_cell(row, indexes["Date"], "Date"), "Date")
        if trade_date.year != month.year or trade_date.month != month.month:
            raise DataSourceContractError(f"{trade_date} is outside the requested month")
        if trade_date in volumes:
            raise DataSourceContractError(f"trading highlights repeated {trade_date}")
        volume = _parse_int(_cell(row, indexes["Trade Volume"], "Trade Volume"), "Trade Volume")
        if volume < 0:
            raise DataSourceContractError(f"{trade_date} has a negative trade volume")
        volumes[trade_date] = (
            volume,
            _parse_decimal(_cell(row, indexes["TAIEX"], "TAIEX"), "TAIEX"),
        )
    return volumes


def build_taiex_daily_bars(
    *,
    month: date,
    index_history: Mapping[date, tuple[Decimal, Decimal, Decimal, Decimal]],
    trading_volumes: Mapping[date, tuple[int, Decimal]],
    fetched_at: datetime,
) -> TaiexDailyBars:
    """Join the two reports on trade date.

    The index report decides which sessions exist, because a bar without a
    close cannot be stored at all. Both reports carry the closing index, so
    disagreement between them means the two requests straddled a correction or
    we are reading the wrong column; either way the month is not trustworthy.
    """
    items: list[TaiexDailyBar] = []
    for trade_date in sorted(index_history):
        open_index, high, low, close = index_history[trade_date]
        if close <= 0:
            raise DataSourceContractError(f"{trade_date} closed at {close}")
        if high < low:
            raise DataSourceContractError(f"{trade_date} has high {high} below low {low}")
        volume: int | None = None
        published = trading_volumes.get(trade_date)
        if published is not None:
            volume, reported_close = published
            if reported_close != close:
                raise DataSourceContractError(
                    f"{trade_date} closed at {close} in the index report but "
                    f"{reported_close} in the trading highlights"
                )
        items.append(
            TaiexDailyBar(
                trade_date=trade_date,
                open=open_index,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    return TaiexDailyBars(month=month, items=tuple(items), fetched_at=fetched_at)


def parse_market_flows(
    payload: Mapping[str, Any], *, trade_date: date, fetched_at: datetime
) -> TwseMarketFlows:
    rows = _rows(payload, trade_date)
    if rows is None:
        return TwseMarketFlows(trade_date=trade_date, items=(), fetched_at=fetched_at)
    index = _field_indexes(payload, MARKET_FLOW_FIELDS)
    items: list[TwseMarketFlow] = []
    for row in rows:
        label = _cell(row, index["單位名稱"], "單位名稱")
        if label == MARKET_FLOW_TOTAL_LABEL:
            continue
        investor_type = MARKET_FLOW_INVESTORS.get(label)
        if investor_type is None:
            # A renamed or new category must surface, not be dropped silently.
            raise DataSourceContractError(f"unknown investor label {label!r}")
        buy = _parse_int(_cell(row, index["買進金額"], "買進金額"), "買進金額")
        sell = _parse_int(_cell(row, index["賣出金額"], "賣出金額"), "賣出金額")
        net = _parse_int(_cell(row, index["買賣差額"], "買賣差額"), "買賣差額")
        _check_net(buy, sell, net, label)
        items.append(TwseMarketFlow(investor_type, buy, sell, net))
    # Every investor must be present exactly once: a short day would still be
    # stored, and `stored_flow_dates` only asks whether a date has any rows, so
    # nothing would ever come back to fill the gap.
    if sorted(item.investor_type for item in items) != sorted(MARKET_FLOW_INVESTORS.values()):
        raise DataSourceContractError("investor rows are missing or duplicated")
    return TwseMarketFlows(trade_date=trade_date, items=tuple(items), fetched_at=fetched_at)


def parse_stock_flows(
    payload: Mapping[str, Any], *, trade_date: date, fetched_at: datetime
) -> TwseStockFlows:
    rows = _rows(payload, trade_date)
    if rows is None:
        return TwseStockFlows(trade_date=trade_date, items=(), fetched_at=fetched_at)
    required = [STOCK_SYMBOL_FIELD, STOCK_NAME_FIELD]
    for columns in STOCK_FLOW_COLUMNS.values():
        required.extend(columns)
    index = _field_indexes(payload, required)
    items: list[TwseStockFlow] = []
    seen: set[str] = set()
    for row in rows:
        symbol = _cell(row, index[STOCK_SYMBOL_FIELD], STOCK_SYMBOL_FIELD)
        name = _cell(row, index[STOCK_NAME_FIELD], STOCK_NAME_FIELD)
        if not isinstance(symbol, str) or not isinstance(name, str):
            raise DataSourceContractError("symbol or name is not a string")
        symbol = symbol.strip()
        name = name.strip()
        if not symbol or symbol in seen:
            raise DataSourceContractError(f"blank or duplicate symbol {symbol!r}")
        seen.add(symbol)
        for investor_type, (buy_field, sell_field, net_field) in STOCK_FLOW_COLUMNS.items():
            buy = _parse_int(_cell(row, index[buy_field], buy_field), buy_field)
            sell = _parse_int(_cell(row, index[sell_field], sell_field), sell_field)
            net = _parse_int(_cell(row, index[net_field], net_field), net_field)
            _check_net(buy, sell, net, f"{symbol} {investor_type}")
            items.append(TwseStockFlow(symbol, name, investor_type, buy, sell, net))
    return TwseStockFlows(trade_date=trade_date, items=tuple(items), fetched_at=fetched_at)


class TwseAdapter:
    """One-date-per-request client that spaces its own requests.

    The spacing lives here rather than in the caller's loop so every path that
    talks to TWSE is throttled the same way. Requests are issued sequentially;
    the run queue already guarantees one institutional run at a time.

    The interval is measured from when a request finishes, not from when it
    starts, so a slow or timed-out request cannot consume the gap that follows
    it. Each transient failure adds another interval on top, so a walk that
    runs into a 429 or an outage keeps backing further off instead of asking
    the same rate for its next 80 dates.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 10.0,
        request_interval_seconds: float = 6.0,
        max_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds must not be negative")
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        self._interval = request_interval_seconds
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None
        self._backoff_intervals = 0
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout_seconds)
        )

    async def __aenter__(self) -> "TwseAdapter":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_market_flows(self, trade_date: date) -> TwseMarketFlows:
        payload, fetched_at = await self._get(
            MARKET_FLOWS_PATH,
            {"response": "json", "type": "day", "dayDate": trade_date.strftime("%Y%m%d")},
        )
        return parse_market_flows(payload, trade_date=trade_date, fetched_at=fetched_at)

    async def get_stock_flows(self, trade_date: date) -> TwseStockFlows:
        payload, fetched_at = await self._get(
            STOCK_FLOWS_PATH,
            {
                "response": "json",
                "date": trade_date.strftime("%Y%m%d"),
                "selectType": STOCK_FLOWS_SELECT_TYPE,
            },
        )
        return parse_stock_flows(payload, trade_date=trade_date, fetched_at=fetched_at)

    async def get_taiex_daily_bars(self, month: date) -> TaiexDailyBars:
        """One calendar month of TAIEX bars, from the two reports that hold them.

        Both requests go through the same spacing as every other TWSE call, so
        a month costs two intervals. `month` may be any date inside the month.
        """
        requested = month.strftime("%Y%m%d")
        index_payload, index_fetched_at = await self._get(
            TAIEX_INDEX_PATH, {"response": "json", "date": requested}
        )
        volume_payload, _ = await self._get(
            TAIEX_TRADING_PATH, {"response": "json", "date": requested}
        )
        return build_taiex_daily_bars(
            month=month,
            index_history=parse_taiex_index_history(index_payload, month=month),
            trading_volumes=parse_taiex_trading_volumes(volume_payload, month=month),
            fetched_at=index_fetched_at,
        )

    async def _get(self, path: str, params: Mapping[str, str]) -> tuple[dict[str, Any], datetime]:
        """Retry transient failures in place; a lost date is only refetched on
        the next run, and runs are triggered by hand."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await self._request(path, params)
            except DataSourceTransientError:
                if attempt == self._max_attempts:
                    raise
        raise AssertionError("bounded twse request loop exited unexpectedly")

    async def _request(
        self, path: str, params: Mapping[str, str]
    ) -> tuple[dict[str, Any], datetime]:
        if self._last_request_at is not None:
            elapsed = self._monotonic() - self._last_request_at
            wait = self._interval * (1 + self._backoff_intervals) - elapsed
            if wait > 0:
                await self._sleep(wait)
        try:
            response = await self._client.get(
                path, params=params, headers={"Accept": "application/json"}
            )
        except httpx.HTTPError as error:
            self._backoff_intervals = min(self._backoff_intervals + 1, MAX_BACKOFF_INTERVALS)
            raise DataSourceTransientError("twse request failed") from error
        finally:
            self._last_request_at = self._monotonic()
        fetched_at = datetime.now(UTC)
        if response.status_code >= 500 or response.status_code == 429:
            self._backoff_intervals = min(self._backoff_intervals + 1, MAX_BACKOFF_INTERVALS)
            raise DataSourceTransientError(f"twse responded {response.status_code}")
        # The server answered, so whatever it was backing off from is over.
        self._backoff_intervals = 0
        if response.status_code != 200:
            raise DataSourceContractError(f"twse responded {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise DataSourceContractError("twse response is not JSON") from error
        if not isinstance(payload, dict):
            raise DataSourceContractError("twse response is not an object")
        return payload, fetched_at
