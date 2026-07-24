from typing import Final, NamedTuple


class MarketDefinition(NamedTuple):
    code: str
    name_en: str
    name_zh_hant: str
    name_zh_hans: str


MARKETS: Final[tuple[MarketDefinition, ...]] = (
    MarketDefinition(
        "global_macro_bonds",
        "US Macro and Global Bonds",
        "美國宏觀及全球債市",
        "美国宏观及全球债市",
    ),
    MarketDefinition("crypto", "Cryptocurrency", "加密貨幣", "加密货币"),
    MarketDefinition("forex", "Foreign Exchange", "外匯", "外汇"),
    MarketDefinition("us_equity", "US Equities", "美股", "美股"),
    MarketDefinition("hk_equity", "Hong Kong Equities", "港股", "港股"),
    MarketDefinition("cn_equity", "Mainland China Equities", "陸股", "陆股"),
    MarketDefinition("tw_equity", "Taiwan Equities", "台股", "台股"),
    MarketDefinition(
        "tw_index_derivatives",
        "Taiwan Index Futures and Options",
        "台灣指數期貨與選擇權",
        "台湾指数期货与期权",
    ),
)
