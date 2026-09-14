import re
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

import daily_insights_api.modules.news.feeds as feeds
from daily_insights_api.modules.news.contracts import Candidate, Selection
from daily_insights_api.modules.news.editions import (
    EDITION_ORDER,
    EDITION_SPECS,
    GLOBAL_SPEC,
    MARKET_NEWS_CODES,
    TW_EQUITY_SPEC,
    US_EQUITY_SPEC,
    edition_spec,
)
from daily_insights_api.modules.news.extraction import FetchedCandidate
from daily_insights_api.modules.news.feeds import (
    FEED_SOURCES,
    discover_feed_candidates,
    effective_hostnames,
)
from daily_insights_api.modules.news.llm import (
    MARKET_VALUES,
    enforce_selection_policy,
    filter_selection_markets,
    selection_output_contract,
)

ALLOWED = effective_hostnames()


def _fetched(index: int, host: str) -> FetchedCandidate:
    url = f"https://{host}/story-{index}"
    return FetchedCandidate(
        Candidate(
            id=f"{index:064x}",
            url=url,
            hostname=host,
            source_name=host,
            headline=f"Story {index}",
        ),
        url,
        f"Body {index}",
        f"{index:064x}",
    )


def _selection(ids: list[int], *, topic: str = "markets", market: str = "taiwan") -> Selection:
    return Selection.model_validate(
        {
            "selections": [
                {
                    "id": f"{index:064x}",
                    "topic": topic,
                    "event_key": f"event-{index}",
                    "market": market,
                    "importance": 3,
                }
                for index in ids
            ]
        }
    )


def test_edition_registry_is_ordered_global_first_and_targets_match_policies() -> None:
    assert EDITION_ORDER == ("global", "tw_equity", "us_equity")
    assert MARKET_NEWS_CODES == ("tw_equity", "us_equity")
    for spec in EDITION_SPECS.values():
        assert spec.target_items == spec.selection.max_items == 5
        assert spec.selection.min_four_star_items == 5
        assert spec.selection.max_four_star_items == 10
        assert spec.selection.max_low_importance_items == 5
        assert spec.max_candidates >= spec.target_items
        assert spec.max_per_source >= 1
        assert spec.headline_impact_patterns
        assert spec.selection.importance_guidance
        for pattern in spec.headline_impact_patterns:
            re.compile(pattern)
    for spec in (TW_EQUITY_SPEC, US_EQUITY_SPEC):
        assert spec.max_candidates > GLOBAL_SPEC.max_candidates
        assert spec.max_discovery_per_source > GLOBAL_SPEC.max_discovery_per_source
        assert spec.max_discovery_total > GLOBAL_SPEC.max_discovery_total
    assert edition_spec("us_equity") is US_EQUITY_SPEC
    with pytest.raises(ValueError, match="unknown news edition market"):
        edition_spec("fx")


def test_market_policies_allow_a_single_source_and_single_market() -> None:
    candidates = [_fetched(index, "news.cnyes.com") for index in range(1, 6)]
    same_topic = _selection(list(range(1, 6)))
    with pytest.raises(ValueError, match="2 topics"):
        enforce_selection_policy(same_topic, candidates, TW_EQUITY_SPEC.selection)

    diverse = Selection.model_validate(
        {
            "selections": [
                {**item.model_dump(), "topic": "companies" if index % 2 else "markets"}
                for index, item in enumerate(same_topic.selections)
            ]
        }
    )
    # Five Taiwan stories from one domain and one market satisfy the market policy...
    enforce_selection_policy(diverse, candidates, TW_EQUITY_SPEC.selection)
    # ...but the global digest keeps its two-per-domain rule and the US edition
    # its three-per-domain rule.
    with pytest.raises(ValueError, match="2 stories per domain"):
        enforce_selection_policy(diverse, candidates, GLOBAL_SPEC.selection)
    with pytest.raises(ValueError, match="3 stories per domain"):
        enforce_selection_policy(diverse, candidates, US_EQUITY_SPEC.selection)
    # Low-importance stories share one five-story ceiling.
    eight = [_fetched(index, "news.cnyes.com") for index in range(1, 9)]
    with pytest.raises(ValueError, match="exceeds 5 stories rated 1 to 3"):
        enforce_selection_policy(_selection(list(range(1, 9))), eight, TW_EQUITY_SPEC.selection)
    with pytest.raises(ValueError, match="unknown candidate ID"):
        enforce_selection_policy(diverse, candidates[:2], TW_EQUITY_SPEC.selection)


def _global_or_us_selection(ids: list[int]) -> Selection:
    return Selection.model_validate(
        {
            "selections": [
                {
                    "id": f"{index:064x}",
                    "topic": "companies" if index % 2 else "markets",
                    "event_key": f"event-{index}",
                    "market": "us",
                    "importance": 3,
                }
                for index in ids
            ]
        }
    )


def test_full_edition_must_spread_across_three_source_domains() -> None:
    # The US edition's five-story baseline must span at least three outlets.
    hosts = ["a.example"] * 4 + ["b.example"] * 4 + ["c.example"] * 4
    candidates = [_fetched(index, host) for index, host in enumerate(hosts, start=1)]
    two_domains = Selection(
        selections=tuple(
            item.model_copy(update={"importance": 4})
            for item in _global_or_us_selection(list(range(1, 9))).selections
        )
    )
    with pytest.raises(ValueError, match="3 stories per domain"):
        enforce_selection_policy(two_domains, candidates, US_EQUITY_SPEC.selection)
    # Taiwan deliberately allows a single source; global and US retain spread.
    assert GLOBAL_SPEC.selection.min_domains_full == 3
    assert TW_EQUITY_SPEC.selection.min_domains_full == 1


def test_output_contract_reflects_each_policy() -> None:
    global_contract = selection_output_contract(GLOBAL_SPEC.selection)
    market_contract = selection_output_contract(TW_EQUITY_SPEC.selection)
    assert "0 to 20 objects" in global_contract["selections"]
    assert "every qualifying 5-star story" in global_contract["selections"]
    assert "at most 10" in global_contract["selections"]
    assert "5 stories rated 1 to 3" in global_contract["selections"]
    assert "event_keys unique" in global_contract["selections"]
    assert "span at least 3 distinct source domains" in global_contract["selections"]
    assert "source domains" not in market_contract["selections"].split("topics")[-1]
    # Every edition offers the full market vocabulary so the model classifies
    # each story by the market it is really about; the rule then names the
    # only tag the edition publishes.
    assert "distinct markets" not in global_contract["selections"]
    assert global_contract["market"] == MARKET_VALUES
    assert "publishes only selections tagged 'global'" in global_contract["market_rule"]
    assert "0 to 30 objects" in market_contract["selections"]
    assert "every qualifying 5-star story" in market_contract["selections"]
    assert "distinct markets" not in market_contract["selections"]
    assert market_contract["market"] == MARKET_VALUES
    assert "publishes only selections tagged 'taiwan'" in market_contract["market_rule"]
    assert "never relabel a story to fit" in market_contract["market_rule"]
    assert "absolute Taiwan-equity scale" in market_contract["importance"]
    us_contract = selection_output_contract(US_EQUITY_SPEC.selection)
    assert "absolute US-equity scale" in us_contract["importance"]
    assert "systemic catalyst" in global_contract["importance"]
    assert (
        len(
            {
                global_contract["importance"],
                market_contract["importance"],
                us_contract["importance"],
            }
        )
        == 3
    )


def test_feed_sources_are_tagged_per_market() -> None:
    by_market: dict[str, set[str]] = {}
    for source in FEED_SOURCES:
        for market in source.markets:
            by_market.setdefault(market, set()).add(source.hostname)
    # The global digest reads English-native publishers only; Chinese,
    # Japanese and Korean media carry their own dormant market tags.
    assert {"www.theguardian.com", "www.cnbc.com", "www.thestreet.com"} <= by_market["global"]
    assert not (
        {"www.cls.cn", "www.etnet.com.hk", "www.hankyung.com", "news.cnyes.com"}
        & by_market["global"]
    )
    assert "www.cls.cn" in by_market["cn_equity"]
    assert "www.etnet.com.hk" in by_market["hk_equity"]
    assert "www.hankyung.com" in by_market["kr_equity"]
    # Taiwanese media feed the Taiwan edition; cnyes' international desk and the
    # English sources feed the US edition.
    assert {"news.cnyes.com", "money.udn.com"} <= by_market["tw_equity"]
    assert {"news.cnyes.com", "www.cnbc.com", "www.globenewswire.com"} <= by_market["us_equity"]
    assert "www.sec.gov" in by_market["us_equity"] and "www.sec.gov" not in by_market["global"]
    assert all(market in EDITION_SPECS for market in by_market if market in EDITION_ORDER)


async def test_discovery_reads_only_feeds_tagged_for_the_requested_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def robots(_: httpx.AsyncClient, __: str, ___: frozenset[str]) -> bool:
        return True

    monkeypatch.setattr(feeds, "robots_allowed", robots)
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text="<html></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await discover_feed_candidates(
            client, ALLOWED, datetime(2026, 9, 2, tzinfo=UTC), market="tw_equity"
        )
    expected = [source.url for source in FEED_SOURCES if "tw_equity" in source.markets]
    assert requested == expected
    assert "https://news.cnyes.com/rss/v1/news/category/tw_stock" in requested
    assert not any("wsj" in url or "guardianapis" in url for url in requested)


def test_market_editions_drop_stories_tagged_for_other_markets() -> None:
    selection = Selection.model_validate(
        {
            "selections": [
                {
                    "id": "a" * 64,
                    "topic": "markets",
                    "event_key": "taiex-close",
                    "market": "taiwan",
                    "importance": 4,
                },
                {
                    "id": "b" * 64,
                    "topic": "economy",
                    "event_key": "el-nino",
                    "market": "global",
                    "importance": 2,
                },
                {
                    "id": "c" * 64,
                    "topic": "companies",
                    "event_key": "tsmc-capex",
                    "market": "taiwan",
                    "importance": 5,
                },
            ]
        }
    )

    kept, dropped = filter_selection_markets(selection, TW_EQUITY_SPEC.selection)

    assert [item.event_key for item in kept.selections] == ["taiex-close", "tsmc-capex"]
    assert [item.market for item in dropped] == ["global"]
    # The global digest keeps only region-neutral stories.
    global_kept, global_dropped = filter_selection_markets(selection, GLOBAL_SPEC.selection)
    assert [item.event_key for item in global_kept.selections] == ["el-nino"]
    assert [item.market for item in global_dropped] == ["taiwan", "taiwan"]


def test_output_contract_names_the_published_tag_and_gate_for_each_edition() -> None:
    focus_by_tag: dict[str, str] = {}
    for spec, tag in ((TW_EQUITY_SPEC, "taiwan"), (US_EQUITY_SPEC, "us"), (GLOBAL_SPEC, "global")):
        contract = selection_output_contract(spec.selection)
        assert contract["market"] == MARKET_VALUES
        assert f"tagged '{tag}'" in contract["market_rule"]
        assert contract["example"]["selections"][0]["market"] == tag
        focus = spec.selection.market_focus
        assert focus is not None
        focus_by_tag[tag] = focus
        # Each edition spells out its relevance gate and tells the model to
        # tag off-market stories honestly rather than force its own tag.
        assert "relevance gate" in focus
        assert f"only '{tag}' stories are published" in focus.lower()
    assert "macro" in focus_by_tag["global"]
    assert "TWSE or TPEx listed company" in focus_by_tag["taiwan"]
    assert "US-listed company" in focus_by_tag["us"]
    # Without a published set there is nothing to enforce, so no rule is shown.
    open_policy = replace(GLOBAL_SPEC.selection, allowed_markets=None)
    assert "market_rule" not in selection_output_contract(open_policy)


def test_repair_promotes_diverse_reserve_without_relaxing_full_edition_policy() -> None:
    from daily_insights_api.modules.news.llm import repair_selection_policy

    hosts = ["a.example"] * 3 + ["b.example"] * 3 + ["c.example"]
    candidates = [_fetched(index, host) for index, host in enumerate(hosts, start=1)]
    original = _global_or_us_selection(list(range(1, 8)))
    repaired = repair_selection_policy(original, candidates, US_EQUITY_SPEC.selection)
    assert [item.id for item in repaired.selections] == [f"{i:064x}" for i in [1, 2, 3, 4, 7]]
    enforce_selection_policy(repaired, candidates, US_EQUITY_SPEC.selection)
    # With no diverse reserve, a valid partial remains preferable to no news.
    partial = repair_selection_policy(
        _global_or_us_selection(list(range(1, 7))), candidates, US_EQUITY_SPEC.selection
    )
    assert len(partial.selections) == 4
    enforce_selection_policy(partial, candidates, US_EQUITY_SPEC.selection)


def test_publication_reconsiders_successful_stories_when_refill_supplies_missing_topic() -> None:
    from daily_insights_api.modules.news.llm import publishable_selection

    candidates = [_fetched(i, f"source-{i}.example") for i in range(1, 6)]
    same_topic = list(_selection([1, 2, 3, 4], market="global").selections)
    assert len(publishable_selection(same_topic, candidates, GLOBAL_SPEC.selection).selections) == 2
    refill = _selection([5], market="global", topic="economy").selections
    publication = publishable_selection(
        same_topic + list(refill), candidates, GLOBAL_SPEC.selection
    )
    assert len(publication.selections) == 5
    enforce_selection_policy(publication, candidates, GLOBAL_SPEC.selection)


def test_publication_enforces_domain_cap_across_more_than_one_selection_batch() -> None:
    from daily_insights_api.modules.news.llm import publishable_selection

    candidates = [
        _fetched(i, "a.example" if i < 9 else "b.example" if i < 12 else "c.example")
        for i in range(1, 13)
    ]
    ranked = list(_global_or_us_selection(list(range(1, 9))).selections)
    ranked += list(_global_or_us_selection(list(range(9, 13))).selections)
    publication = publishable_selection(ranked, candidates, US_EQUITY_SPEC.selection)
    assert len(publication.selections) == 5
    assert [item.id for item in publication.selections] == [f"{i:064x}" for i in [1, 2, 3, 9, 12]]
    enforce_selection_policy(publication, candidates, US_EQUITY_SPEC.selection)


def test_every_market_publishes_all_fives_and_caps_lower_importance_tiers() -> None:
    from daily_insights_api.modules.news.llm import publishable_selection

    candidates = [_fetched(index, f"source-{index}.example") for index in range(1, 35)]
    ranked = Selection.model_validate(
        {
            "selections": [
                {
                    "id": f"{index:064x}",
                    "topic": "policy" if index % 2 else "markets",
                    "event_key": f"quota-event-{index}",
                    "market": "global",
                    "importance": 5 if index <= 12 else 4 if index <= 24 else 2,
                }
                for index in range(1, 35)
            ]
        }
    ).selections

    for spec in EDITION_SPECS.values():
        publication = publishable_selection(list(ranked), candidates, spec.selection)
        assert sum(item.importance == 5 for item in publication.selections) == 12
        assert sum(item.importance == 4 for item in publication.selections) == 10
        assert sum(item.importance <= 3 for item in publication.selections) == 5
