"""Edition specifications: the global daily digest plus per-market news editions.

Every edition shares the same pipeline (discover, extract, select, summarize,
persist) but differs in which discovery paths and feeds it reads, how many
stories it targets, and how strictly the selection must spread across
sources, topics, and markets.
"""

from dataclasses import dataclass

GLOBAL_MARKET = "global"
MARKET_NEWS_CODES: tuple[str, ...] = ("tw_equity", "us_equity")

GLOBAL_HEADLINE_IMPACT_PATTERNS = (
    r"\b(?:fed(?:eral reserve)?|ecb|boj|bank of japan|rba|central bank)\b"
    r"|央行|聯準會|欧洲央行|歐洲央行|日本銀行|日本银行",
    r"\b(?:rate decision|raise rates?|rate hikes?|rate cuts?|monetary policy)\b"
    r"|利率決策|利率决策|升息|加息|降息|貨幣政策|货币政策",
    r"\b(?:ppi|cpi|pce|gdp|payrolls?|inflation|unemployment|employment|jobs report)\b"
    r"|通膨|通胀|國內生產毛額|国内生产总值|非農|非农",
    r"\b(?:u\.?s\.?\s+)?treasur(?:y|ies)\b|\b(?:government|sovereign|longer-term) debt\b"
    r"|\bbond(?:s|-market)?\b|\byields?\b|美債|美债|公債|国债|殖利率|收益率",
    r"\b(?:buy[- ]?backs?|debt purchases?|bond purchases?|auctions?|liquidity operations?)\b"
    r"|回購|回购|標售|招標|流動性操作|流动性操作",
    r"\b(?:brent|crude)\b|\boil(?:\s+prices?)?\b|\bgas prices?\b"
    r"|原油|油價|油价|能源價格|能源价格",
    r"\b(?:military conflict|armed conflict|war|iran|attacks?|destroys?)\b"
    r"|戰爭|战争|伊朗|攻擊|攻击|摧毀|摧毁|軍事衝突|军事冲突",
    r"\b(?:tariffs?|import bans?|export bans?|sanctions?)\b"
    r"|關稅|关税|進口禁令|进口禁令|出口禁令|制裁",
    r"\b(?:trade war|escalat(?:e|es|ed|ing|ion))\b|貿易戰|贸易战|升級|升级",
    r"\b(?:dow(?: jones)?|s&p 500|nasdaq|global (?:stocks?|markets?))\b"
    r"|\b(?:stock market|market-wide)\b|全球股市|全球市場|全球市场",
)

TW_EQUITY_HEADLINE_IMPACT_PATTERNS = (
    r"\b(?:taiex|twse|tpex|taiwan weighted|msci taiwan)\b"
    r"|台股|臺股|加權指數|櫃買|集中市場|店頭市場",
    r"\b(?:tsmc|mediatek|hon hai|foxconn|umc|quanta)\b"
    r"|台積電|臺積電|聯發科|鴻海|聯電|廣達",
    r"\b(?:semiconductor|chip|foundry|ai server)\b"
    r"|半導體|晶片|晶圓代工|先進製程|先進封裝|AI伺服器|AI 伺服器",
    r"\b(?:earnings|revenue|guidance|orders?|capacity|capex|dividend)\b"
    r"|財報|營收|獲利|展望|訂單|產能|資本支出|股利|法說",
    r"\b(?:foreign institutional|institutional flows?|margin financing)\b"
    r"|外資|投信|自營商|三大法人|融資|融券",
    r"\b(?:taiwan central bank|fsc taiwan)\b|央行|金管會|證交所|櫃買中心",
    r"\b(?:tariffs?|export controls?|sanctions?)\b|關稅|关税|出口管制|制裁",
)

US_EQUITY_HEADLINE_IMPACT_PATTERNS = (
    r"\b(?:s&p 500|nasdaq|dow(?: jones)?|russell 2000|wall street)\b"
    r"|標普|标普|那斯達克|纳斯达克|道瓊|道琼|華爾街|华尔街",
    r"\b(?:fed(?:eral reserve)?|fomc|powell)\b|聯準會|联准会|聯儲|美聯儲|美联储|鮑爾|鲍威尔",
    r"\b(?:cpi|ppi|pce|payrolls?|jobs report|gdp|ism|retail sales|inflation)\b"
    r"|通膨|通胀|非農|非农|就業報告|就业报告|零售銷售|零售销售",
    r"\b(?:u\.?s\.?\s+)?treasur(?:y|ies)\b|\byields?\b|\bdollar\b"
    r"|美債|美债|殖利率|收益率|美元",
    r"\b(?:apple|microsoft|nvidia|amazon|alphabet|meta|tesla|broadcom|intel|amd|"
    r"tsmc|qualcomm|micron|coinbase)\b"
    r"|蘋果|苹果|微軟|微软|輝達|英偉達|英伟达|亞馬遜|亚马逊|谷歌|特斯拉|"
    r"英特爾|英特尔|超微|台積電|臺積電|高通|美光|Coinbase",
    r"\b(?:semiconductors?|chips?|foundr(?:y|ies)|ai capex|ai infrastructure|cpus?|gpus?)\b"
    r"|半導體|半导体|晶片|晶圆|晶圓|AI資本支出|AI 資本支出|CPU|GPU",
    r"\b(?:earnings|revenue|guidance|merger|acquisition|antitrust|sec)\b"
    r"|財報|财报|營收|营收|展望|併購|并购|反壟斷|反垄断",
    r"\b(?:shares?|stocks?)\b.{0,32}\b(?:surge|jump|rally|gain|plunge|slide|fall)s?\b"
    r"|\b(?:surge|jump|rally|gain|plunge|slide|fall)s?\b.{0,32}\b(?:shares?|stocks?)\b"
    r"|股價.{0,16}(?:大漲|上漲|狂飆|重挫|下跌)|(?:大漲|狂飆|重挫).{0,16}股價",
    r"\b(?:price hikes?|raises? prices?|product price increases?)\b|漲價|涨价|調漲價格|调涨价格",
    r"\b(?:tariffs?|trade policy|export controls?|sanctions?)\b"
    r"|關稅|关税|貿易政策|贸易政策|出口管制|制裁|進口禁令|进口禁令|貿易戰|贸易战",
)


@dataclass(frozen=True)
class SelectionPolicy:
    """Limits the model's selection must satisfy for one edition."""

    max_items: int
    max_returned_items: int
    max_per_domain: int
    min_topics: int
    min_markets: int
    min_four_star_items: int = 5
    max_four_star_items: int = 10
    max_low_importance_items: int = 5
    market_focus: str | None = None
    # Tags this edition publishes. The model classifies every pick by the
    # market it is really about, using the full vocabulary, and anything
    # tagged outside this set is dropped before the limits are enforced, so
    # an off-market story costs one slot rather than being relabelled to fit.
    # None means any tag is published.
    allowed_markets: frozenset[str] | None = None
    # Source spread required once every edition slot is filled: the first
    # max_items picks must come from at least this many source domains. 1
    # disables the rule for editions that deliberately allow one source.
    min_domains_full: int = 1
    # Market-specific absolute importance scale. Each edition owns this
    # contract so a five-star Taiwan story need not satisfy the global
    # cross-market definition, and future editions can define their own scale.
    importance_guidance: str | None = None

    @property
    def selection_limit(self) -> int:
        return self.max_returned_items


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
    # Broad headline signals used only to rank candidates before extraction
    # and model selection. Each regex is one independent impact dimension;
    # matching more dimensions ranks a headline earlier without auto-publishing
    # it or assigning its final importance.
    headline_impact_patterns: tuple[str, ...] = ()

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
    headline_impact_patterns=GLOBAL_HEADLINE_IMPACT_PATTERNS,
    selection=SelectionPolicy(
        max_items=5,
        max_returned_items=20,
        max_per_domain=2,
        min_topics=2,
        min_markets=1,
        min_domains_full=3,
        market_focus=(
            "This is the global macro digest for a cross-market audience: central bank "
            "decisions and guidance, inflation and growth data, rates and yields, FX, "
            "energy and commodities, geopolitical or trade events with market-wide impact, "
            "cross-border capital flows, and sovereign debt supply, auctions, buybacks or "
            "liquidity operations. First identify the day's independent dominant macro "
            "themes across all candidates, then rank the originating policy, data, supply "
            "or conflict catalyst ahead of its market reactions and company-level "
            "consequences. Preserve coverage across independent themes before adding a "
            "second story from one theme. Major-central-bank official guidance or a "
            "high-credibility consensus showing an imminent policy-path shift is a macro "
            "catalyst, not commentary. Pure price-action reports about FX, bonds, indexes, "
            "gold, silver or oil fail the relevance gate when they add no new policy, data, "
            "supply or conflict development. A company transaction, product, investment, "
            "executive comment, payment technology or sector trend fails the gate when it "
            "does not itself change global growth, inflation, rates, currencies, sovereign "
            "debt, energy supply or trade conditions, regardless of company fame or deal "
            "size. Do not spend multiple slots on reaction stories tied to one catalyst. "
            "A story passes the relevance gate only when its impact reaches investors "
            "across regions; tag those stories market 'global'. "
            "A story that matters mainly to one country or region (a local listed "
            "company, a domestic policy, one exchange's session) fails the gate and must "
            "not be selected even if the remaining candidates are weaker; tag it with "
            "its own region instead of forcing 'global'. Only 'global' stories are "
            "published."
        ),
        # The digest is deliberately region-neutral: only cross-market stories.
        allowed_markets=frozenset({"global"}),
        importance_guidance=(
            "integer on an absolute scale, the same on every day and in every batch: "
            "5 = a systemic catalyst with immediate cross-region or cross-asset impact, "
            "such as a major central-bank decision, inflation shock, sovereign debt "
            "supply or liquidity intervention, energy shock, military escalation, or "
            "trade restriction; 4 = significant for investors across multiple markets, "
            "including official forward guidance or a high-credibility consensus showing "
            "an imminent shift in a major central bank's policy path; 3 = notable but "
            "narrow; 2 = minor; 1 = trivial. An analyst recommendation, investment-bank or "
            "CEO opinion, or market outlook without a new official policy action or data "
            "release is at most 3. Pure price action with no new macro development and "
            "standalone company products, investments, transactions or sector themes fail "
            "this global edition's relevance gate rather than filling an empty slot. "
            "Rate honestly: a story never earns a higher rating because slots are empty, "
            "and most days have few or no 5s"
        ),
    ),
)

# Market editions use the same importance-tier quotas but screen a wider pool than the global
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
    headline_impact_patterns=TW_EQUITY_HEADLINE_IMPACT_PATTERNS,
    selection=SelectionPolicy(
        max_items=MARKET_EDITION_ITEMS,
        max_returned_items=MARKET_EDITION_CANDIDATES,
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
        importance_guidance=(
            "integer on an absolute Taiwan-equity scale, the same on every day and in "
            "every batch: 5 = likely to move the TAIEX, a major sector, or several large "
            "Taiwan-listed companies immediately, such as a Taiwan central-bank or market "
            "policy decision, a major foreign-flow reversal, or a material semiconductor "
            "supply-chain shock; 4 = significant for a major Taiwan sector or leading "
            "index constituent; 3 = material to one listed company or a narrow industry; "
            "2 = minor; 1 = trivial. Rank the Taiwan market consequence, not the overseas "
            "headline's fame. Routine product launches, commentary and small contracts are "
            "at most 3 unless the article demonstrates a broader Taiwan price impact. Rate "
            "honestly and never raise a rating because edition slots are empty"
        ),
    ),
)

US_EQUITY_SPEC = EditionSpec(
    market_code="us_equity",
    target_items=MARKET_EDITION_ITEMS,
    max_candidates=MARKET_EDITION_CANDIDATES,
    max_per_source=MARKET_EDITION_PER_SOURCE,
    max_discovery_per_source=MARKET_EDITION_PER_SOURCE,
    max_discovery_total=MARKET_EDITION_DISCOVERY_TOTAL,
    headline_impact_patterns=US_EQUITY_HEADLINE_IMPACT_PATTERNS,
    selection=SelectionPolicy(
        max_items=MARKET_EDITION_ITEMS,
        max_returned_items=MARKET_EDITION_CANDIDATES,
        max_per_domain=3,
        min_topics=2,
        min_markets=1,
        min_domains_full=3,
        market_focus=(
            "US equities. The governing question is whether the event can change the "
            "pricing of broad US indexes, an important US sector, or a sufficiently "
            "weighted US-listed company. A story passes the relevance gate only when "
            "its main subject is one of: the S&P 500, Nasdaq, Dow or Russell indexes and "
            "their session; a US-listed company with a material earnings, guidance, "
            "competitive, regulatory, capital-allocation or share-price catalyst; Federal "
            "Reserve policy or US macro data such as payrolls, CPI, PCE, GDP or ISM; US "
            "Treasury yields, the dollar, oil, tariffs or trade policy with a direct US "
            "equity transmission; or an overseas event whose effect on named US-listed "
            "companies or a major US sector the article itself spells out. For example, "
            "a TSMC result qualifies when the article establishes a direct consequence "
            "for US-listed AI or semiconductor companies. Tag those stories market 'us'. "
            "Keep this edition differentiated from the global macro digest: when enough "
            "qualifying candidates exist, use roughly 30-40% market-wide drivers and "
            "60-70% company or sector equity catalysts among selections below 5 stars. "
            "Never force that mix by omitting a genuine 5-star event, inflating a weak "
            "company story, or selecting an off-market story. Evaluate event certainty "
            "and prefer the primary event: an enacted rule or official action outranks a "
            "CEO's expectation, a company disclosure outranks an analyst forecast, and a "
            "Fed or Treasury action outranks an investment bank's interpretation. A "
            "material analyst view may qualify but must not displace a stronger primary "
            "catalyst. Apply a freshness penalty to company stories that are materially "
            "older than the current candidate batch, especially when more than roughly "
            "24 hours old and carrying no substantive update. Fails the gate "
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
        importance_guidance=(
            "integer on an absolute US-equity scale, the same on every day and in every "
            "batch: 5 = likely to move broad US indexes or several major sectors "
            "immediately, such as a Fed decision, major US macro surprise, Treasury-market "
            "shock, systemic regulation, or exceptional mega-cap development; 4 = "
            "significant for a major US sector or leading index constituent; 3 = material "
            "to one listed company or a narrow industry; 2 = minor; 1 = trivial. Rank the "
            "originating catalyst above a session recap that merely reports its reaction. "
            "An analyst, investment-bank or CEO forecast or opinion without a new primary "
            "event is at most 3. Routine product launches and ordinary deals are also at "
            "most 3 unless the article demonstrates broad index or sector impact. Routine "
            "financing--including senior-note or bond issuance, refinancing, secondary "
            "offerings and tender offers--is at most 2 unless the size is exceptional "
            "relative to the company, it signals financial stress, causes material "
            "dilution, constitutes a credit event, finances a major acquisition, or the "
            "article demonstrates a significant share-price reaction. A large US "
            "semiconductor catalyst combining a material product-pricing change, strategic "
            "relationship and significant share move can be 4 when it changes sector or "
            "leading-constituent pricing. Rate honestly and never raise a rating because "
            "edition slots are empty"
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
