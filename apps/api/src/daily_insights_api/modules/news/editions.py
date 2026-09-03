"""Edition specifications: the global daily digest plus per-market news editions.

Every edition shares the same pipeline (discover, extract, select, summarize,
persist) but differs in which discovery paths and feeds it reads, how many
stories it targets, and how strictly the selection must spread across
sources, topics, and markets.
"""

from dataclasses import dataclass

GLOBAL_MARKET = "global"
MARKET_NEWS_CODES: tuple[str, ...] = ("tw_equity", "us_equity")


@dataclass(frozen=True)
class SelectionPolicy:
    """Limits the model's selection must satisfy for one edition."""

    max_items: int
    max_per_domain: int
    min_topics: int
    min_markets: int
    market_focus: str | None = None


@dataclass(frozen=True)
class EditionSpec:
    market_code: str
    target_items: int
    max_candidates: int
    max_per_source: int
    max_discovery_per_source: int
    selection: SelectionPolicy
    # Upper bound on articles extracted per run; full-text candidates are kept
    # first because they cost no fetch.
    max_discovery_total: int = 80

    @property
    def is_global(self) -> bool:
        return self.market_code == GLOBAL_MARKET


GLOBAL_SPEC = EditionSpec(
    market_code=GLOBAL_MARKET,
    target_items=5,
    max_candidates=20,
    max_per_source=5,
    max_discovery_per_source=5,
    selection=SelectionPolicy(max_items=5, max_per_domain=2, min_topics=2, min_markets=2),
)

TW_EQUITY_SPEC = EditionSpec(
    market_code="tw_equity",
    target_items=8,
    max_candidates=24,
    max_per_source=8,
    max_discovery_per_source=8,
    selection=SelectionPolicy(
        max_items=8,
        max_per_domain=8,
        min_topics=2,
        min_markets=1,
        market_focus=(
            "Taiwan equities: TWSE and TPEx listed companies, TAIEX and Taiwan index "
            "futures, foreign institutional flows, the semiconductor and electronics "
            "supply chain, Taiwan central bank and FSC policy, and global events with a "
            "direct Taiwan market impact. Use market 'taiwan' for Taiwan-specific stories."
        ),
    ),
)

US_EQUITY_SPEC = EditionSpec(
    market_code="us_equity",
    target_items=8,
    max_candidates=24,
    max_per_source=8,
    max_discovery_per_source=8,
    selection=SelectionPolicy(
        max_items=8,
        max_per_domain=4,
        min_topics=2,
        min_markets=1,
        market_focus=(
            "US equities: S&P 500, Nasdaq and Dow moves, listed-company earnings and "
            "guidance, Federal Reserve policy, US macro data, and sector or mega-cap "
            "developments. Use market 'us' for US-specific stories."
        ),
    ),
)

EDITION_SPECS: dict[str, EditionSpec] = {
    spec.market_code: spec for spec in (GLOBAL_SPEC, TW_EQUITY_SPEC, US_EQUITY_SPEC)
}
EDITION_ORDER: tuple[str, ...] = tuple(EDITION_SPECS)


def edition_spec(market_code: str) -> EditionSpec:
    try:
        return EDITION_SPECS[market_code]
    except KeyError as error:
        raise ValueError(f"unknown news edition market: {market_code}") from error
