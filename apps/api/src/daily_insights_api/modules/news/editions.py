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
    # Market editions accept only their own market tag; anything else the
    # model picks is dropped before the limits are enforced. None means any.
    allowed_markets: frozenset[str] | None = None


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
    selection=SelectionPolicy(
        max_items=5,
        max_per_domain=2,
        min_topics=2,
        min_markets=2,
        market_focus=(
            "This is the global macro digest for a cross-market audience: central bank "
            "decisions and guidance, inflation and growth data, rates and yields, FX, "
            "energy and commodities, geopolitical or trade events with market-wide impact, "
            "and cross-border capital flows. Single-company or single-country stories "
            "qualify only when they move more than their home market; the country "
            "editions cover the rest."
        ),
    ),
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
            "direct Taiwan market impact. Use market 'taiwan' for Taiwan-specific stories. "
            "Hard rule: a story with no direct link to Taiwan-listed companies or the "
            "Taiwan market (weather, entertainment, general science, lifestyle) must not "
            "be selected even if every other candidate is weaker; leave the slot empty."
        ),
        allowed_markets=frozenset({"taiwan"}),
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
            "developments. Use market 'us' for US-specific stories. Hard rule: a story "
            "with no direct link to US-listed companies or US markets must not be "
            "selected; leave the slot empty instead."
        ),
        allowed_markets=frozenset({"us"}),
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
