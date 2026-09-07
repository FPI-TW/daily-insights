from collections.abc import AsyncIterator
from typing import Protocol

from daily_insights_api.modules.data_sources.dto import (
    DailyBar,
    DailyBarQuery,
    Instrument,
    InstrumentQuery,
    ProviderPage,
)


class MarketDataProvider(Protocol):
    async def get_instruments(self, query: InstrumentQuery) -> ProviderPage[Instrument]: ...

    async def iter_instrument_pages(
        self,
        query: InstrumentQuery,
        *,
        max_pages: int = 1000,
    ) -> AsyncIterator[ProviderPage[Instrument]]: ...

    async def get_daily_bars(self, query: DailyBarQuery) -> ProviderPage[DailyBar]: ...

    async def iter_daily_bar_pages(
        self,
        query: DailyBarQuery,
        *,
        max_pages: int = 1000,
    ) -> AsyncIterator[ProviderPage[DailyBar]]: ...
