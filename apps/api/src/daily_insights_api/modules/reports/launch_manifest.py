import hashlib
import json
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
    status: Literal["draft", "approved"]
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
        if any(
            not block.datasets or len(set(block.datasets)) != len(block.datasets)
            for market in self.markets
            for block in market.blocks
        ):
            raise ValueError("every block must reference one or more unique datasets")
        if any(not dataset.required_fields for dataset in self.datasets):
            raise ValueError("every dataset must freeze required fields")
        block_ids = [block.id for market in self.markets for block in market.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("manifest block ids must be globally unique")
        if any(
            set(block.labels) != {"zh-hant", "zh-hans", "en"}
            for market in self.markets
            for block in market.blocks
        ):
            raise ValueError("every block must freeze exactly three locale labels")
        if any(
            len({key for block in market.blocks for key in block.datasets}) != 1
            for market in self.markets
        ):
            raise ValueError("each first-wave market must use one atomic dataset")
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
    version="three-market.v1",
    provider="twelve_data",
    status="draft",
    markets=(
        MarketManifest(
            market_code="global_macro_bonds",
            blocks=(
                BlockManifest(
                    id="macro.commodities",
                    kind="metric",
                    datasets=("macro.commodity_quotes",),
                    formula="provider close and percent_change; no substitution",
                    unit_code="provider_quote_currency",
                    precision=4,
                    labels={
                        "zh-hant": "商品快照",
                        "zh-hans": "商品快照",
                        "en": "Commodity snapshot",
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
        MarketManifest(
            market_code="us_equity",
            blocks=(
                BlockManifest(
                    id="us.market_movers",
                    kind="table",
                    datasets=("us.market_movers",),
                    formula="top two gainers followed by top two losers as returned by provider",
                    unit_code="usd_percent",
                    precision=4,
                    labels={
                        "zh-hant": "市場領漲與領跌",
                        "zh-hans": "市场领涨与领跌",
                        "en": "Market movers",
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
            required_fields=("close", "percent_change", "currency"),
            timezone="provider exchange timezone; aware timestamp required",
            day_boundary="provider quote calendar date",
            freshness="latest completed provider quote",
        ),
        DatasetManifest(
            key="crypto.daily_bars",
            endpoint="/time_series",
            symbols=("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD"),
            required_fields=("datetime", "open", "high", "low", "close", "volume"),
            minimum_history=485,
            timezone="provider exchange timezone from meta.exchange_timezone",
            day_boundary="provider daily-bar calendar date",
            freshness="latest completed 1day bar",
        ),
        DatasetManifest(
            key="us.market_movers",
            endpoint="/market_movers/stocks",
            symbols=(),
            required_fields=(
                "symbol",
                "name",
                "last",
                "percent_change",
                "volume",
            ),
            timezone="aware provider mover timestamp",
            day_boundary="provider mover calendar date",
            freshness="current provider market-movers snapshot",
        ),
    ),
)
