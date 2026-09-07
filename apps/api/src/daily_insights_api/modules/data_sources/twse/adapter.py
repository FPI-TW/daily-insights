"""TWSE rwd institutional-investor adapter: BFI82U (market, TWD) and T86 (per stock, shares).

Both endpoints take exactly one trading date; there is no range form. A date
with no trading returns HTTP 200 and a Chinese `stat` message instead of a 4xx,
so "has data" is `stat == "OK"` together with a non-empty `data` list. Row order
and row count in BFI82U changed across the years, so every value is looked up
by field name, never by position.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from daily_insights_api.modules.data_sources.errors import (
    DataSourceContractError,
    DataSourceTransientError,
)

logger = logging.getLogger(__name__)

MARKET_FLOWS_PATH = "/rwd/zh/fund/BFI82U"
STOCK_FLOWS_PATH = "/rwd/zh/fund/T86"
# `ALL` also returns ~15k warrant rows; this keeps the ~1.3k securities.
STOCK_FLOWS_SELECT_TYPE = "ALLBUT0999"
NO_DATA_STAT_MARKER = "沒有符合條件"

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


def _parse_int(value: object, field: str) -> int:
    if not isinstance(value, str):
        raise DataSourceContractError(f"{field} is not a string")
    try:
        return int(value.replace(",", "").strip())
    except ValueError as error:
        raise DataSourceContractError(f"{field} is not an integer") from error


def _rows(payload: Mapping[str, Any], trade_date: date) -> list[list[Any]] | None:
    """Return the data rows, or None when TWSE says the date had no trading."""
    stat = payload.get("stat")
    if stat != "OK":
        if isinstance(stat, str) and NO_DATA_STAT_MARKER in stat:
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
    if len({item.investor_type for item in items}) != len(items):
        raise DataSourceContractError("duplicate investor rows")
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
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 10.0,
        request_interval_seconds: float = 6.0,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds must not be negative")
        self._interval = request_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None
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

    async def _get(self, path: str, params: Mapping[str, str]) -> tuple[dict[str, Any], datetime]:
        if self._last_request_at is not None:
            wait = self._interval - (self._monotonic() - self._last_request_at)
            if wait > 0:
                await self._sleep(wait)
        self._last_request_at = self._monotonic()
        try:
            response = await self._client.get(
                path, params=params, headers={"Accept": "application/json"}
            )
        except httpx.HTTPError as error:
            raise DataSourceTransientError("twse request failed") from error
        fetched_at = datetime.now(UTC)
        if response.status_code >= 500 or response.status_code == 429:
            raise DataSourceTransientError(f"twse responded {response.status_code}")
        if response.status_code != 200:
            raise DataSourceContractError(f"twse responded {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise DataSourceContractError("twse response is not JSON") from error
        if not isinstance(payload, dict):
            raise DataSourceContractError("twse response is not an object")
        return payload, fetched_at
