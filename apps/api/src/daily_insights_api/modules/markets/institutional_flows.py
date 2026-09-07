import asyncio
from datetime import date, timedelta
from time import monotonic

from daily_insights_api.modules.data_sources.dto import (
    InstitutionalFlowResult,
    InstitutionalStockFlowResult,
)
from daily_insights_api.modules.data_sources.provider import InstitutionalFlowProvider


class TwseInstitutionalFlowService:
    def __init__(
        self, provider: InstitutionalFlowProvider, *, current_ttl_seconds: int = 300
    ) -> None:
        self._provider = provider
        self._current_ttl_seconds = current_ttl_seconds
        self._daily_cache: dict[date, tuple[float, InstitutionalFlowResult]] = {}
        self._stock_cache: dict[tuple[date, str], tuple[float, InstitutionalStockFlowResult]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        close = getattr(self._provider, "close", None)
        if close is not None:
            await close()

    async def daily_flows(self, start: date, end: date) -> tuple[InstitutionalFlowResult, ...]:
        days: list[date] = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                days.append(current)
            current += timedelta(days=1)
        results = await asyncio.gather(*(self._daily(day) for day in days))
        return tuple(result for result in results if result.item is not None)

    async def latest_stock_flows(
        self, requested_date: date, *, locale: str
    ) -> InstitutionalStockFlowResult:
        for offset in range(8):
            candidate = requested_date - timedelta(days=offset)
            if candidate.weekday() >= 5:
                continue
            result = await self._stocks(candidate, locale)
            if result.items:
                return result
        return result

    async def _daily(self, trade_date: date) -> InstitutionalFlowResult:
        key = f"daily:{trade_date.isoformat()}"
        cached = self._daily_cache.get(trade_date)
        if cached is not None and self._fresh(trade_date, cached[0]):
            return cached[1]
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = self._daily_cache.get(trade_date)
            if cached is not None and self._fresh(trade_date, cached[0]):
                return cached[1]
            result = await self._provider.get_daily_flow(trade_date)
            self._daily_cache[trade_date] = (monotonic(), result)
            return result

    async def _stocks(self, trade_date: date, locale: str) -> InstitutionalStockFlowResult:
        cache_key = (trade_date, locale)
        key = f"stocks:{trade_date.isoformat()}:{locale}"
        cached = self._stock_cache.get(cache_key)
        if cached is not None and self._fresh(trade_date, cached[0]):
            return cached[1]
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = self._stock_cache.get(cache_key)
            if cached is not None and self._fresh(trade_date, cached[0]):
                return cached[1]
            result = await self._provider.get_stock_flows(trade_date, locale=locale)
            self._stock_cache[cache_key] = (monotonic(), result)
            return result

    def _fresh(self, trade_date: date, stored_at: float) -> bool:
        return trade_date < date.today() or monotonic() - stored_at < self._current_ttl_seconds
