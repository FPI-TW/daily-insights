"""Public provider-neutral data-source contracts."""

from daily_insights_api.modules.data_sources import (
    DailyBar,
    DailyBarQuery,
    DataSourceAuthenticationError,
    DataSourceContractError,
    DataSourceError,
    DataSourceTransientError,
    Instrument,
    InstrumentQuery,
    PageInfo,
    Provenance,
    ProviderPage,
    UnsupportedMarketError,
)
from daily_insights_api.modules.data_sources.dto import MarketCode
from daily_insights_api.modules.data_sources.provider import MarketDataProvider
from daily_insights_api.modules.data_sources.twelve_data import (
    TWELVE_DATA_CONTRACT_HASH,
    TWELVE_DATA_CONTRACT_VERSION,
    EodResult,
    EodsResult,
    QuoteResult,
    QuotesResult,
    RetryPolicy,
    TwelveDataAdapter,
    TwelveDataTransport,
)
from daily_insights_api.modules.data_sources.yfinance import (
    TRACKED_INDICES,
    IndexSymbol,
    YfinanceAdapter,
    YfinanceDailyBars,
)

__all__ = [
    "TRACKED_INDICES",
    "TWELVE_DATA_CONTRACT_HASH",
    "TWELVE_DATA_CONTRACT_VERSION",
    "DailyBar",
    "DailyBarQuery",
    "DataSourceAuthenticationError",
    "DataSourceContractError",
    "DataSourceError",
    "DataSourceTransientError",
    "EodResult",
    "EodsResult",
    "IndexSymbol",
    "Instrument",
    "InstrumentQuery",
    "MarketCode",
    "MarketDataProvider",
    "PageInfo",
    "Provenance",
    "ProviderPage",
    "QuoteResult",
    "QuotesResult",
    "RetryPolicy",
    "TwelveDataAdapter",
    "TwelveDataTransport",
    "UnsupportedMarketError",
    "YfinanceAdapter",
    "YfinanceDailyBars",
]
