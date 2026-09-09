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
    # Tags this edition publishes. The model classifies every pick by the
    # market it is really about, using the full vocabulary, and anything
    # tagged outside this set is dropped before the limits are enforced, so
    # an off-market story costs one slot rather than being relabelled to fit.
    # None means any tag is published.
    allowed_markets: frozenset[str] | None = None
    # Extra ranked picks the model may return beyond max_items. They are only
    # summarised when an earlier story fails verification, so a dropped
    # number no longer costs the edition a slot.
    reserve_items: int = 2
    # Source spread required once every edition slot is filled: the first
    # max_items picks must come from at least this many source domains. 1
    # disables the rule for editions that deliberately allow one source.
    min_domains_full: int = 1

    @property
    def selection_limit(self) -> int:
        return self.max_items + self.reserve_items


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
    # Take candidates from sources in turn instead of newest-first overall. The
    # global digest needs this: its flash feeds publish dozens of items an hour
    # and would otherwise fill every slot before a slower wire gets one.
    interleave_sources: bool = False

    @property
    def is_global(self) -> bool:
        return self.market_code == GLOBAL_MARKET


GLOBAL_SPEC = EditionSpec(
    market_code=GLOBAL_MARKET,
    target_items=5,
    max_candidates=20,
    max_per_source=5,
    max_discovery_per_source=5,
    interleave_sources=True,
    selection=SelectionPolicy(
        max_items=5,
        max_per_domain=2,
        min_topics=2,
        min_markets=1,
        min_domains_full=3,
        market_focus=(
            "This is the global macro digest for a cross-market audience: central bank "
            "decisions and guidance, inflation and growth data, rates and yields, FX, "
            "energy and commodities, geopolitical or trade events with market-wide impact, "
            "and cross-border capital flows. Single-company or single-country stories "
            "qualify only when they move more than their home market; the country "
            "editions cover the rest. A story passes the relevance gate only when its "
            "impact reaches investors across regions; tag those stories market 'global'. "
            "A story that matters mainly to one country or region (a local listed "
            "company, a domestic policy, one exchange's session) fails the gate and must "
            "not be selected even if the remaining candidates are weaker; tag it with "
            "its own region instead of forcing 'global'. Only 'global' stories are "
            "published."
        ),
        # The digest is deliberately region-neutral: only cross-market stories.
        allowed_markets=frozenset({"global"}),
    ),
)

# Market editions publish five stories but screen a wider pool than the global
# digest: more articles per source and in total reach the model, so the five
# it keeps are the strongest Taiwan- or US-linked events rather than the first
# eight that arrived. Relevance is the gate; the slots are the ceiling.
MARKET_EDITION_ITEMS = 5
MARKET_EDITION_CANDIDATES = 30
MARKET_EDITION_PER_SOURCE = 10
MARKET_EDITION_DISCOVERY_TOTAL = 100

TW_EQUITY_SPEC = EditionSpec(
    market_code="tw_equity",
    target_items=MARKET_EDITION_ITEMS,
    max_candidates=MARKET_EDITION_CANDIDATES,
    max_per_source=MARKET_EDITION_PER_SOURCE,
    max_discovery_per_source=MARKET_EDITION_PER_SOURCE,
    max_discovery_total=MARKET_EDITION_DISCOVERY_TOTAL,
    selection=SelectionPolicy(
        max_items=MARKET_EDITION_ITEMS,
        max_per_domain=MARKET_EDITION_ITEMS,
        min_topics=2,
        min_markets=1,
        market_focus=(
            "Taiwan equities. A story passes the relevance gate only when its main "
            "subject is one of: a TWSE or TPEx listed company (its results, guidance, "
            "orders, capacity, a major contract or an event that moves its shares); the "
            "TAIEX, Taiwan index futures or Taiwan ETFs; foreign institutional, "
            "investment trust or dealer flows in Taiwan; Taiwan central bank, FSC, "
            "exchange or government policy that affects listed companies; the Taiwan "
            "semiconductor and electronics supply chain; or an overseas event whose "
            "Taiwan market consequence the article itself spells out (for example a "
            "US tariff on Taiwanese exporters, or a customer's order cut for Taiwanese "
            "suppliers). Tag those stories market 'taiwan'. Fails the gate and must not "
            "be selected even when every other candidate is weaker: stories mainly "
            "about US, Chinese, Japanese, Korean or European markets or companies with "
            "no Taiwan-listed counterparty named in the article; macro or commodity "
            "news that never mentions a Taiwan market effect; domestic politics, "
            "weather, crime, entertainment, sport, lifestyle, real estate listings, "
            "personal finance tips, product reviews and general science. Tag a story "
            "with the market it is really about instead of forcing 'taiwan'; only "
            "'taiwan' stories are published."
        ),
        allowed_markets=frozenset({"taiwan"}),
    ),
)

US_EQUITY_SPEC = EditionSpec(
    market_code="us_equity",
    target_items=MARKET_EDITION_ITEMS,
    max_candidates=MARKET_EDITION_CANDIDATES,
    max_per_source=MARKET_EDITION_PER_SOURCE,
    max_discovery_per_source=MARKET_EDITION_PER_SOURCE,
    max_discovery_total=MARKET_EDITION_DISCOVERY_TOTAL,
    selection=SelectionPolicy(
        max_items=MARKET_EDITION_ITEMS,
        max_per_domain=3,
        min_topics=2,
        min_markets=1,
        min_domains_full=3,
        market_focus=(
            "US equities. A story passes the relevance gate only when its main subject "
            "is one of: the S&P 500, Nasdaq, Dow or Russell indexes and their session; a "
            "US-listed company (earnings, guidance, a deal, a regulatory action or an "
            "event that moves its shares); Federal Reserve policy or Fed officials' "
            "guidance; US macro data such as payrolls, CPI, PCE, GDP or ISM; US "
            "Treasury yields, the dollar or US sector and mega-cap developments; US "
            "government, SEC or trade policy that affects US-listed companies; or an "
            "overseas event whose effect on US-listed companies or US markets the "
            "article itself spells out. Tag those stories market 'us'. Fails the gate "
            "and must not be selected even when every other candidate is weaker: "
            "stories mainly about European, Asian or other markets or companies with "
            "no US-listed counterparty named in the article; foreign central banks or "
            "foreign macro data with no stated US market effect; a company's press "
            "release with no market or investor consequence; domestic politics without "
            "a market angle, weather, crime, entertainment, sport, lifestyle, product "
            "reviews and general science. Tag a story with the market it is really "
            "about instead of forcing 'us'; only 'us' stories are published."
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
