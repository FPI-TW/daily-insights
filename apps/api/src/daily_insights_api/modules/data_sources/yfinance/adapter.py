"""Yahoo Finance daily-bar adapter.

`yf.download()` and `yf.Ticker().history()` were probed on 2026-09-03 against
every tracked index under both `period="2y"` and explicit `start`/`end` windows.
They returned identical row counts for every symbol, so this adapter makes one
call rather than choosing between two.
"""

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import yfinance
from pandas import DataFrame, Timestamp

from daily_insights_api.modules.data_sources.dto import DailyBar, MarketCode, Provenance
from daily_insights_api.modules.data_sources.errors import (
    DataSourceContractError,
    DataSourceTransientError,
)

YFINANCE_CONTRACT_VERSION = "2026-09-03.v1"
YFINANCE_CONTRACT_HASH = hashlib.sha256(
    b"yfinance:Ticker.history,interval:1d,actions:false,auto_adjust:false:2026-09-03.v1"
).hexdigest()
YFINANCE_ENDPOINT = "Ticker.history"
REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")

# yfinance caches exchange timezones through peewee/sqlite under the user cache
# directory. The API container runs as `nobody`, which owns no home directory,
# so the location is pinned somewhere writable.
DEFAULT_TIMEZONE_CACHE_DIRECTORY = "/tmp/py-yfinance"


@dataclass(frozen=True, slots=True)
class DailyBarsResult:
    symbol: str
    market: MarketCode
    items: tuple[DailyBar, ...]
    # Yahoo appends the current session's still-open bar to the series. It is
    # excluded from `items` because it carries no settled close, and reported
    # here so callers can see that it was seen and dropped.
    dropped_unsettled_trade_date: date | None
    provenance: Provenance


class YfinanceAdapter:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        timezone_cache_directory: str = DEFAULT_TIMEZONE_CACHE_DIRECTORY,
    ) -> None:
        yfinance.set_tz_cache_location(timezone_cache_directory)
        # Without this yfinance swallows upstream failures and returns an empty
        # frame, which is indistinguishable from a symbol that has no data.
        yfinance.config.debug.hide_exceptions = False
        self._timeout_seconds = timeout_seconds

    async def get_daily_bars(
        self,
        *,
        market: MarketCode,
        symbol: str,
        period: str = "2y",
    ) -> DailyBarsResult:
        # yfinance is synchronous and blocking.
        frame = await asyncio.to_thread(self._history, symbol, period)
        return normalize_daily_bars(
            market=market,
            symbol=symbol,
            period=period,
            frame=frame,
            fetched_at=datetime.now(UTC),
        )

    def _history(self, symbol: str, period: str) -> DataFrame:
        try:
            frame = yfinance.Ticker(symbol).history(
                period=period,
                interval="1d",
                actions=False,
                auto_adjust=False,
                timeout=self._timeout_seconds,
            )
        except Exception as error:
            raise DataSourceTransientError(f"yfinance history failed for {symbol}") from error
        # yfinance is untyped, so the frame is only a DataFrame by convention.
        if not isinstance(frame, DataFrame):
            raise DataSourceContractError(
                f"yfinance history for {symbol} returned {type(frame).__name__}, not a DataFrame"
            )
        return frame


def normalize_daily_bars(
    *,
    market: MarketCode,
    symbol: str,
    period: str,
    frame: DataFrame,
    fetched_at: datetime,
) -> DailyBarsResult:
    """Validate the frame at the trust boundary and map it to normalized DTOs."""
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise DataSourceContractError(
            f"yfinance history for {symbol} omitted columns: {', '.join(missing)}"
        )

    items: list[DailyBar] = []
    dropped_unsettled_trade_date: date | None = None
    for index, row in frame.iterrows():
        if not isinstance(index, Timestamp) or index.tzinfo is None:
            raise DataSourceContractError(
                f"yfinance history for {symbol} returned a naive or non-datetime index"
            )
        trade_date = index.date()
        # The index carries the exchange's own timezone, so "today" is decided
        # there rather than in UTC or in the server's local zone.
        if trade_date >= datetime.now(index.tzinfo).date():
            dropped_unsettled_trade_date = trade_date
            continue
        close = _decimal(row["Close"])
        if close is None:
            raise DataSourceContractError(
                f"yfinance history for {symbol} returned {trade_date} without a close"
            )
        # These mirror the CHECK constraints on index_daily_bars. Rejecting an
        # implausible bar here turns it into a per-symbol contract failure that
        # the caller already handles, instead of an IntegrityError raised on
        # write, which would fail the whole batch.
        if close <= 0:
            raise DataSourceContractError(
                f"yfinance history for {symbol} returned {trade_date} with close {close}"
            )
        high = _decimal(row["High"])
        low = _decimal(row["Low"])
        if high is not None and low is not None and high < low:
            raise DataSourceContractError(
                f"yfinance history for {symbol} returned {trade_date} with high {high} "
                f"below low {low}"
            )
        # Zero is legitimate and common: Yahoo reports no volume at all for some
        # indices (^SOX is zero on every row), so only a negative volume is a
        # contract violation.
        volume = _integer(row["Volume"])
        if volume is not None and volume < 0:
            raise DataSourceContractError(
                f"yfinance history for {symbol} returned {trade_date} with volume {volume}"
            )
        items.append(
            DailyBar(
                instrument_source_id=symbol,
                market=market,
                symbol=symbol,
                trade_date=trade_date,
                open=_decimal(row["Open"]),
                high=high,
                low=low,
                close=close,
                volume=volume,
                source="yfinance",
            )
        )

    if not items:
        raise DataSourceContractError(f"yfinance returned no settled daily bars for {symbol}")

    return DailyBarsResult(
        symbol=symbol,
        market=market,
        items=tuple(items),
        dropped_unsettled_trade_date=dropped_unsettled_trade_date,
        provenance=_provenance(symbol=symbol, period=period, items=items, fetched_at=fetched_at),
    )


def _provenance(
    *,
    symbol: str,
    period: str,
    items: list[DailyBar],
    fetched_at: datetime,
) -> Provenance:
    query = json.dumps(
        [
            ("actions", False),
            ("auto_adjust", False),
            ("interval", "1d"),
            ("period", period),
            ("symbol", symbol),
        ],
        separators=(",", ":"),
    ).encode()
    payload = json.dumps(
        [bar.model_dump(mode="json") for bar in items],
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return Provenance(
        provider="yfinance",
        contract_version=YFINANCE_CONTRACT_VERSION,
        contract_hash=YFINANCE_CONTRACT_HASH,
        endpoint=YFINANCE_ENDPOINT,
        query_fingerprint=hashlib.sha256(query).hexdigest(),
        fetched_at=fetched_at,
        as_of=max(bar.trade_date for bar in items),
        response_digest=hashlib.sha256(payload).hexdigest(),
        record_count=len(items),
    )


def _decimal(value: Any) -> Decimal | None:
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return Decimal(str(number))


def _integer(value: Any) -> int | None:
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return int(number)
