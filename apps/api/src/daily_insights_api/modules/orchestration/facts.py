import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.data_sources.api import DailyBar
from daily_insights_api.modules.orchestration.models import (
    FunctionRun,
    InterestRateObservation,
    InterestRateSeries,
    MarketDailyObservation,
    MarketDailySeries,
)


def fact_digest(values: dict[str, object]) -> str:
    material = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode()).hexdigest()


async def fence_is_current(
    database: AsyncSession, *, function_run_id: uuid.UUID, fence_token: uuid.UUID
) -> bool:
    return bool(
        await database.scalar(
            select(FunctionRun.id)
            .where(
                FunctionRun.id == function_run_id,
                FunctionRun.status == "running",
                FunctionRun.lease_token == fence_token,
            )
            .with_for_update()
        )
    )


async def store_market_bars(
    database: AsyncSession,
    *,
    function_run_id: uuid.UUID,
    fence_token: uuid.UUID,
    function_attempt_id: uuid.UUID,
    provider_key: str,
    dataset_key: str,
    symbol: str,
    market: str,
    unit: str,
    contract_version: str,
    bars: tuple[DailyBar, ...],
    provisional_trade_date: date | None = None,
    source_timestamp: datetime | None = None,
) -> int:
    if not await fence_is_current(
        database, function_run_id=function_run_id, fence_token=fence_token
    ):
        return 0
    series = await database.scalar(
        select(MarketDailySeries).where(
            MarketDailySeries.provider_key == provider_key,
            MarketDailySeries.dataset_key == dataset_key,
            MarketDailySeries.symbol == symbol,
        )
    )
    if series is None:
        series = MarketDailySeries(
            provider_key=provider_key,
            dataset_key=dataset_key,
            symbol=symbol,
            market=market,
            unit=unit,
            contract_version=contract_version,
        )
        database.add(series)
        await database.flush()
    inserted = 0
    for bar in bars:
        if bar.close is None:
            continue
        values: dict[str, object] = {
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "is_provisional": bar.trade_date == provisional_trade_date,
        }
        digest = fact_digest(values)
        latest = await database.scalar(
            select(MarketDailyObservation)
            .where(
                MarketDailyObservation.series_id == series.id,
                MarketDailyObservation.observation_date == bar.trade_date,
            )
            .order_by(MarketDailyObservation.version.desc())
            .limit(1)
            .with_for_update()
        )
        if latest is not None and latest.value_digest == digest:
            continue
        database.add(
            MarketDailyObservation(
                series_id=series.id,
                function_attempt_id=function_attempt_id,
                observation_date=bar.trade_date,
                version=1 if latest is None else latest.version + 1,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                is_provisional=bar.trade_date == provisional_trade_date,
                source_timestamp=source_timestamp,
                value_digest=digest,
            )
        )
        inserted += 1
    return inserted


async def latest_market_date(
    database: AsyncSession, *, provider_key: str, dataset_key: str, symbol: str
) -> date | None:
    return cast(
        date | None,
        await database.scalar(
            select(func.max(MarketDailyObservation.observation_date))
            .join(MarketDailySeries, MarketDailySeries.id == MarketDailyObservation.series_id)
            .where(
                MarketDailySeries.provider_key == provider_key,
                MarketDailySeries.dataset_key == dataset_key,
                MarketDailySeries.symbol == symbol,
            )
        ),
    )


async def interest_rate_month_coverage(
    database: AsyncSession,
    *,
    provider_key: str,
    dataset_key: str,
    symbols: tuple[str, ...],
    start_year: int,
    end_year: int,
) -> dict[tuple[int, int], set[str]]:
    rows = await database.execute(
        select(
            func.extract("year", InterestRateObservation.observation_date).label("year"),
            func.extract("month", InterestRateObservation.observation_date).label("month"),
            InterestRateSeries.symbol,
        )
        .join(
            InterestRateSeries,
            InterestRateSeries.id == InterestRateObservation.series_id,
        )
        .where(
            InterestRateSeries.provider_key == provider_key,
            InterestRateSeries.dataset_key == dataset_key,
            InterestRateSeries.symbol.in_(symbols),
            InterestRateObservation.observation_date >= date(start_year, 1, 1),
            InterestRateObservation.observation_date <= date(end_year, 12, 31),
        )
        .distinct()
    )
    coverage: dict[tuple[int, int], set[str]] = {}
    for year, month, symbol in rows:
        coverage.setdefault((int(year), int(month)), set()).add(cast(str, symbol))
    return coverage


async def store_interest_rates(
    database: AsyncSession,
    *,
    function_run_id: uuid.UUID,
    fence_token: uuid.UUID,
    function_attempt_id: uuid.UUID,
    provider_key: str,
    dataset_key: str,
    symbol: str,
    market: str,
    unit: str,
    contract_version: str,
    values: tuple[tuple[date, Decimal], ...],
    source_timestamp: datetime | None = None,
) -> int:
    if not await fence_is_current(
        database, function_run_id=function_run_id, fence_token=fence_token
    ):
        return 0
    series = await database.scalar(
        select(InterestRateSeries).where(
            InterestRateSeries.provider_key == provider_key,
            InterestRateSeries.dataset_key == dataset_key,
            InterestRateSeries.symbol == symbol,
        )
    )
    if series is None:
        series = InterestRateSeries(
            provider_key=provider_key,
            dataset_key=dataset_key,
            symbol=symbol,
            market=market,
            unit=unit,
            contract_version=contract_version,
        )
        database.add(series)
        await database.flush()
    inserted = 0
    for observation_date, value in values:
        digest = fact_digest({"value": value})
        latest = await database.scalar(
            select(InterestRateObservation)
            .where(
                InterestRateObservation.series_id == series.id,
                InterestRateObservation.observation_date == observation_date,
            )
            .order_by(InterestRateObservation.version.desc())
            .limit(1)
            .with_for_update()
        )
        if latest is not None and latest.value_digest == digest:
            continue
        database.add(
            InterestRateObservation(
                series_id=series.id,
                function_attempt_id=function_attempt_id,
                observation_date=observation_date,
                version=1 if latest is None else latest.version + 1,
                value=value,
                source_timestamp=source_timestamp,
                value_digest=digest,
            )
        )
        inserted += 1
    return inserted
