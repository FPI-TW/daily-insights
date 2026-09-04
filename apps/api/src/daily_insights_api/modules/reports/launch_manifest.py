import hashlib
import json
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

LaunchMarketCode = Literal["global_macro_bonds", "crypto", "us_equity"]
LAUNCH_MARKET_ORDER: tuple[LaunchMarketCode, ...] = (
    "global_macro_bonds",
    "crypto",
    "us_equity",
)


class ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetManifest(ManifestModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")
    endpoint: Literal["/quote", "/time_series", "/market_movers/stocks"]
    symbols: tuple[str, ...]
    symbol_units: dict[str, str]
    # Provider "type" query parameter per symbol. Twelve Data reuses tickers
    # across asset classes (HG1 is a German stock unless type=commodity), so
    # ambiguous symbols must pin their class here.
    symbol_types: dict[str, str] = Field(default_factory=dict)
    expected_asset_types: dict[str, str] = Field(default_factory=dict)
    required_fields: tuple[str, ...]
    minimum_history: int = Field(default=1, ge=1, le=5_000)
    timezone: str
    day_boundary: str
    freshness: str
    atomicity: Literal["all_or_error"] = "all_or_error"


class BlockManifest(ManifestModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")
    kind: Literal["metric", "table", "series"]
    datasets: tuple[str, ...]
    formula: str
    unit_code: str
    precision: int = Field(ge=0, le=12)
    rounding: Literal["ROUND_HALF_EVEN"] = "ROUND_HALF_EVEN"
    labels: dict[Literal["zh-hant", "zh-hans", "en"], str]


class MarketManifest(ManifestModel):
    market_code: LaunchMarketCode
    blocks: tuple[BlockManifest, ...]


class LaunchManifest(ManifestModel):
    version: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")
    provider: Literal["twelve_data"]
    markets: tuple[MarketManifest, ...]
    datasets: tuple[DatasetManifest, ...]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if tuple(market.market_code for market in self.markets) != LAUNCH_MARKET_ORDER:
            raise ValueError("manifest markets must match the fixed launch order")
        dataset_keys = [dataset.key for dataset in self.datasets]
        if len(dataset_keys) != len(set(dataset_keys)):
            raise ValueError("manifest dataset keys must be unique")
        referenced = [
            key for market in self.markets for block in market.blocks for key in block.datasets
        ]
        if set(referenced) != set(dataset_keys):
            raise ValueError("manifest blocks must exactly cover declared datasets")
        if any(len(block.datasets) != 1 for market in self.markets for block in market.blocks):
            raise ValueError("every block must reference exactly one dataset")
        dataset_markets = {
            key: {
                market.market_code
                for market in self.markets
                for block in market.blocks
                if key in block.datasets
            }
            for key in dataset_keys
        }
        if any(len(markets) != 1 for markets in dataset_markets.values()):
            raise ValueError("every dataset must be referenced by exactly one market")
        if any(not dataset.required_fields for dataset in self.datasets):
            raise ValueError("every dataset must freeze required fields")
        if any(set(dataset.symbol_units) != set(dataset.symbols) for dataset in self.datasets):
            raise ValueError("every dataset must freeze one unit for each exact symbol")
        if any(
            dataset.expected_asset_types
            and set(dataset.expected_asset_types) != set(dataset.symbols)
            for dataset in self.datasets
        ):
            raise ValueError("asset-type contracts must cover every exact dataset symbol")
        if any(not set(dataset.symbol_types) <= set(dataset.symbols) for dataset in self.datasets):
            raise ValueError("symbol type overrides must name declared dataset symbols")
        if any(
            re.fullmatch(r"[A-Z]{3}", unit) is None
            for dataset in self.datasets
            for unit in dataset.symbol_units.values()
        ):
            raise ValueError("every symbol unit must use a three-letter uppercase code")
        block_ids = [block.id for market in self.markets for block in market.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("manifest block ids must be globally unique")
        if any(
            set(block.labels) != {"zh-hant", "zh-hans", "en"}
            for market in self.markets
            for block in market.blocks
        ):
            raise ValueError("every block must freeze exactly three locale labels")
        return self

    @property
    def sha256(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(canonical).hexdigest()


ACTIVE_LAUNCH_MANIFEST = LaunchManifest(
    version="three-market.v6",
    provider="twelve_data",
    markets=(
        MarketManifest(
            market_code="global_macro_bonds",
            blocks=(
                BlockManifest(
                    id="macro.commodities",
                    kind="metric",
                    datasets=("macro.commodity_quotes",),
                    formula=(
                        "provider quote close; percent change="
                        "(close-previous_close)/previous_close*100; no substitution"
                    ),
                    unit_code="provider_quote_currency",
                    precision=4,
                    labels={
                        "zh-hant": "商品快照",
                        "zh-hans": "商品快照",
                        "en": "Commodity snapshot",
                    },
                ),
                BlockManifest(
                    id="macro.rates_fx",
                    kind="metric",
                    datasets=("macro.rates_fx_quotes",),
                    formula=(
                        "fixed basket of Treasury and dollar-index ETFs plus USD/TWD, USD/JPY "
                        "and EUR/USD spot; latest provider quote close; percent change="
                        "(close-previous_close)/previous_close*100"
                    ),
                    unit_code="provider_quote_currency",
                    precision=4,
                    labels={
                        "zh-hant": "利率與匯率",
                        "zh-hans": "利率与汇率",
                        "en": "Rates and FX",
                    },
                ),
                BlockManifest(
                    id="macro.commodity_normalized_performance",
                    kind="series",
                    datasets=("macro.commodity_daily_bars",),
                    formula=(
                        "normalized close=close/first_close*100 independently over the latest "
                        "30 exact common provider calendar dates; null/zero protected"
                    ),
                    unit_code="index",
                    precision=4,
                    labels={
                        "zh-hant": "布蘭特原油與黃金標準化表現",
                        "zh-hans": "布兰特原油与黄金标准化表现",
                        "en": "Brent and gold normalized performance",
                    },
                ),
            ),
        ),
        MarketManifest(
            market_code="crypto",
            blocks=(
                BlockManifest(
                    id="crypto.overview",
                    kind="table",
                    datasets=("crypto.daily_bars",),
                    formula="latest close; daily change=(close/open-1)*100",
                    unit_code="usd_percent",
                    precision=4,
                    labels={
                        "zh-hant": "加密資產總覽",
                        "zh-hans": "加密资产总览",
                        "en": "Crypto overview",
                    },
                ),
                BlockManifest(
                    id="crypto.normalized_performance",
                    kind="series",
                    datasets=("crypto.daily_bars",),
                    formula="normalized close=close/first_close*100 over the last 30 observations",
                    unit_code="index",
                    precision=4,
                    labels={
                        "zh-hant": "標準化表現",
                        "zh-hans": "标准化表现",
                        "en": "Normalized performance",
                    },
                ),
            ),
        ),
        # A fixed basket replaces provider-ranked market movers: ranking the whole
        # US universe by percent move surfaces only sub-$5 names, which is a
        # property of the endpoint rather than a bug.
        MarketManifest(
            market_code="us_equity",
            blocks=(
                BlockManifest(
                    id="us.index_proxies",
                    kind="metric",
                    datasets=("us.index_proxy_quotes",),
                    formula=(
                        "fixed basket of four index-proxy ETFs and VIXY; latest provider quote "
                        "close; percent change=(close-previous_close)/previous_close*100"
                    ),
                    unit_code="usd",
                    precision=2,
                    labels={
                        "zh-hant": "美股指數快照",
                        "zh-hans": "美股指数快照",
                        "en": "US index snapshot",
                    },
                ),
                BlockManifest(
                    id="us.mega_caps",
                    kind="table",
                    datasets=("us.mega_cap_quotes",),
                    formula=(
                        "fixed basket of eight mega-cap stocks sorted by percent change "
                        "descending; latest provider quote close; percent change="
                        "(close-previous_close)/previous_close*100"
                    ),
                    unit_code="usd_percent",
                    precision=2,
                    labels={
                        "zh-hant": "權值股",
                        "zh-hans": "权值股",
                        "en": "Mega caps",
                    },
                ),
            ),
        ),
    ),
    datasets=(
        DatasetManifest(
            key="macro.commodity_quotes",
            endpoint="/quote",
            symbols=("XBR/USD", "XAU/USD", "HG1"),
            symbol_units={"XBR/USD": "USD", "XAU/USD": "USD", "HG1": "USD"},
            # Without type=commodity the provider resolves HG1 to Homag Group AG
            # (Frankfurt, EUR); the commodity class is copper spot quoted in USD.
            symbol_types={"HG1": "commodity"},
            required_fields=("close", "previous_close", "timestamp"),
            timezone="UTC derived from provider Unix timestamp",
            day_boundary="UTC calendar date of provider timestamp",
            freshness="latest completed provider quote",
        ),
        # Treasury ETFs stand in for yields (the provider has no exact curve
        # symbols) and UUP for the dollar index; FX pairs quote in the second
        # currency of the pair.
        DatasetManifest(
            key="macro.rates_fx_quotes",
            endpoint="/quote",
            symbols=("TLT", "IEF", "UUP", "USD/TWD", "USD/JPY", "EUR/USD"),
            symbol_units={
                "TLT": "USD",
                "IEF": "USD",
                "UUP": "USD",
                "USD/TWD": "TWD",
                "USD/JPY": "JPY",
                "EUR/USD": "USD",
            },
            required_fields=("close", "previous_close", "timestamp"),
            timezone="UTC derived from provider Unix timestamp",
            day_boundary="UTC calendar date of provider timestamp",
            freshness="latest completed provider quote",
        ),
        DatasetManifest(
            key="crypto.daily_bars",
            endpoint="/time_series",
            symbols=("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD"),
            symbol_units={
                "BTC/USD": "USD",
                "ETH/USD": "USD",
                "SOL/USD": "USD",
                "XRP/USD": "USD",
                "ADA/USD": "USD",
            },
            required_fields=("datetime", "open", "high", "low", "close"),
            minimum_history=485,
            timezone="UTC per provider crypto time-series contract",
            day_boundary="provider 1day bar calendar date",
            freshness="latest completed 1day bar",
        ),
        DatasetManifest(
            key="macro.commodity_daily_bars",
            endpoint="/time_series",
            symbols=("XBR/USD", "XAU/USD"),
            symbol_units={"XBR/USD": "USD", "XAU/USD": "USD"},
            expected_asset_types={
                "XBR/USD": "Energy Resource",
                "XAU/USD": "Precious Metal",
            },
            required_fields=("datetime", "open", "high", "low", "close"),
            # The credentialed probe returned 500 rows per symbol. Retaining the full
            # reviewed window leaves ample calendar-overlap headroom above the 30 dates
            # required by the derived series.
            minimum_history=500,
            timezone="provider date-only 1day calendar date",
            day_boundary="provider calendar date; no UTC or exchange timezone inferred",
            freshness="latest completed provider 1day bar; fail if fewer than 30 common dates",
        ),
        DatasetManifest(
            key="us.index_proxy_quotes",
            endpoint="/quote",
            symbols=("SPY", "QQQ", "DIA", "IWM", "VIXY"),
            symbol_units={"SPY": "USD", "QQQ": "USD", "DIA": "USD", "IWM": "USD", "VIXY": "USD"},
            required_fields=("close", "previous_close", "timestamp"),
            timezone="UTC derived from provider Unix timestamp",
            day_boundary="UTC calendar date of provider timestamp",
            freshness="latest completed provider quote",
        ),
        DatasetManifest(
            key="us.mega_cap_quotes",
            endpoint="/quote",
            symbols=("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA"),
            symbol_units={
                "AAPL": "USD",
                "MSFT": "USD",
                "NVDA": "USD",
                "GOOGL": "USD",
                "AMZN": "USD",
                "META": "USD",
                "AVGO": "USD",
                "TSLA": "USD",
            },
            required_fields=("close", "previous_close", "timestamp"),
            timezone="UTC derived from provider Unix timestamp",
            day_boundary="UTC calendar date of provider timestamp",
            freshness="latest completed provider quote",
        ),
    ),
)
