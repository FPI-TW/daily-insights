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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from daily_insights_api.modules.data_sources.dto import DailyBar, MarketCode, Provenance
from daily_insights_api.modules.data_sources.errors import (
    DataSourceContractError,
    DataSourceTransientError,
)

if TYPE_CHECKING:
    from pandas import DataFrame

# yfinance and pandas cost roughly 86MB of resident memory and 0.3s to import.
# This module is reached through data_sources.api by the API process and the
# morning-report scheduler, neither of which touches Yahoo, and the feature is
# off by default, so both imports are deferred to the functions that use them.

YFINANCE_CONTRACT_VERSION = "2026-09-07.v2"
YFINANCE_CONTRACT_HASH = hashlib.sha256(
    b"yfinance:Ticker.history,interval:1d,actions:false,auto_adjust:false:dxy-business-dates:2026-09-07.v2"
).hexdigest()
YFINANCE_ENDPOINT = "Ticker.history"
REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")

# yfinance caches exchange timezones through peewee/sqlite under the user cache
# directory. The API container runs as `nobody`, which owns no home directory,
# so the location is pinned somewhere writable.
DEFAULT_TIMEZONE_CACHE_DIRECTORY = "/tmp/py-yfinance"


@dataclass(frozen=True, slots=True)
class YfinanceDailyBars:
    """Named for its provider: twelve_data.adapter has its own DailyBarsResult,
    and data_sources.api re-exports this one, so the bare name would be
    ambiguous at the shared boundary."""

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
        import yfinance

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
    ) -> YfinanceDailyBars:
        # yfinance is synchronous and blocking.
        frame, regular_market_end = await asyncio.to_thread(self._history, symbol, period)
        fetched_at = datetime.now(UTC)
        return normalize_daily_bars(
            market=market,
            symbol=symbol,
            period=period,
            frame=frame,
            fetched_at=fetched_at,
            regular_market_end=regular_market_end,
        )

    def _history(self, symbol: str, period: str) -> tuple["DataFrame", datetime | None]:
        import yfinance
        from pandas import DataFrame

        try:
            ticker = yfinance.Ticker(symbol)
            frame = ticker.history(
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
        try:
            regular_market_end = _regular_market_end(ticker.history_metadata)
        except Exception:
            # Metadata is advisory and comes from the same undocumented source.
            # A missing or malformed close time must not turn valid historical
            # rows into a failed symbol; normalization conservatively drops a
            # same-day row when it cannot prove the regular session has ended.
            regular_market_end = None
        return frame, regular_market_end


def normalize_daily_bars(
    *,
    market: MarketCode,
    symbol: str,
    period: str,
    frame: "DataFrame",
    fetched_at: datetime,
    regular_market_end: datetime | None = None,
) -> YfinanceDailyBars:
    """Validate the frame at the trust boundary and map it to normalized DTOs."""
    from pandas import Timestamp

    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise DataSourceContractError(
            f"yfinance history for {symbol} omitted columns: {', '.join(missing)}"
        )

    # Yahoo publishes a session's bar before its close is known: the still-open
    # local session mid-morning, and for hours after an Asian market closes its
    # multi-day queries carry open/high/low with a NaN close while its 1d query
    # already has the settled value. Those rows are trailing, so the last row
    # that does have a close marks the end of the settled series. Anything after
    # it has not settled and is dropped; a missing close *before* it is a hole
    # in history rather than a pending one, and still fails the symbol. A frame
    # with no close anywhere is a broken response, not a series of pending days,
    # so it fails too rather than silently normalizing to nothing.
    # `last_valid_index` is typed as any hashable label. Narrowing it the same
    # way the loop below narrows each index keeps the comparison honest, and an
    # index that is not a Timestamp leaves it None, which fails closed.
    last_valid = frame["Close"].last_valid_index()
    last_settled_index = last_valid if isinstance(last_valid, Timestamp) else None

    items: list[DailyBar] = []
    dropped_unsettled_trade_date: date | None = None
    for index, row in frame.iterrows():
        if not isinstance(index, Timestamp) or index.tzinfo is None:
            raise DataSourceContractError(
                f"yfinance history for {symbol} returned a naive or non-datetime index"
            )
        trade_date = index.date()
        # The index carries the exchange's own timezone. A same-day bar is
        # settled once Yahoo's regular session has ended; before then (or when
        # that metadata cannot be trusted) it is excluded conservatively.
        if _is_unsettled_trade_date(
            trade_date=trade_date,
            exchange_timezone=index.tzinfo,
            fetched_at=fetched_at,
            regular_market_end=regular_market_end,
        ):
            dropped_unsettled_trade_date = trade_date
            continue
        # Yahoo's DXY feed includes an unfinished Sunday overnight row whose
        # close is NaN even on Monday. DXY daily closes use business dates;
        # this weekend session must not invalidate settled weekday history.
        if symbol == "DX-Y.NYB" and trade_date.weekday() >= 5:
            continue
        close = _decimal(row["Close"])
        if close is None:
            if last_settled_index is not None and index > last_settled_index:
                dropped_unsettled_trade_date = trade_date
                continue
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

    return YfinanceDailyBars(
        symbol=symbol,
        market=market,
        items=tuple(items),
        dropped_unsettled_trade_date=dropped_unsettled_trade_date,
        provenance=_provenance(symbol=symbol, period=period, items=items, fetched_at=fetched_at),
    )


def _regular_market_end(metadata: Any) -> datetime | None:
    if not isinstance(metadata, Mapping):
        return None
    trading_period = metadata.get("currentTradingPeriod")
    if not isinstance(trading_period, Mapping):
        return None
    regular = trading_period.get("regular")
    if not isinstance(regular, Mapping):
        return None
    value = regular.get("end")
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value


def _is_unsettled_trade_date(
    *,
    trade_date: date,
    exchange_timezone: Any,
    fetched_at: datetime,
    regular_market_end: datetime | None,
) -> bool:
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("fetched_at must include a UTC offset")
    local_fetched_at = fetched_at.astimezone(exchange_timezone)
    if trade_date > local_fetched_at.date():
        return True
    if trade_date < local_fetched_at.date():
        return False
    if (
        regular_market_end is None
        or regular_market_end.tzinfo is None
        or regular_market_end.utcoffset() is None
    ):
        return True
    local_market_end = regular_market_end.astimezone(exchange_timezone)
    return local_market_end.date() != trade_date or fetched_at < regular_market_end


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
