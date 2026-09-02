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
from daily_insights_api.modules.news.feeds import FEED_SOURCES, discover_feed_candidates
from daily_insights_api.modules.news.llm import enforce_selection_policy, selection_output_contract
from daily_insights_api.modules.news.sources import FetchedCandidate, configured_hostnames

ALLOWED = configured_hostnames(
    "www.reuters.com,apnews.com,www.bbc.com,www.cnbc.com,news.cnyes.com,finance.eastmoney.com"
)


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
        assert spec.target_items == spec.selection.max_items
        assert spec.max_candidates >= spec.target_items
        assert spec.max_per_source >= 1
    assert GLOBAL_SPEC.uses_gdelt and not TW_EQUITY_SPEC.uses_gdelt
    assert edition_spec("us_equity") is US_EQUITY_SPEC
    with pytest.raises(ValueError, match="unknown news edition market"):
        edition_spec("fx")


def test_market_policies_allow_a_single_source_and_single_market() -> None:
    candidates = [_fetched(index, "news.cnyes.com") for index in range(1, 9)]
    same_topic = _selection(list(range(1, 9)))
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
    # Eight Taiwan stories from one domain and one market satisfy the market policy...
    enforce_selection_policy(diverse, candidates, TW_EQUITY_SPEC.selection)
    # ...but the global digest keeps its two-per-domain and two-market rules.
    with pytest.raises(ValueError, match="exceeds 5 stories"):
        enforce_selection_policy(diverse, candidates, GLOBAL_SPEC.selection)
    three = Selection.model_validate(
        {"selections": [item.model_dump() for item in diverse.selections[:3]]}
    )
    with pytest.raises(ValueError, match="2 stories per domain"):
        enforce_selection_policy(three, candidates, GLOBAL_SPEC.selection)
    with pytest.raises(ValueError, match="4 stories per domain"):
        enforce_selection_policy(diverse, candidates, US_EQUITY_SPEC.selection)
    with pytest.raises(ValueError, match="unknown candidate ID"):
        enforce_selection_policy(diverse, candidates[:2], TW_EQUITY_SPEC.selection)


def test_output_contract_reflects_each_policy() -> None:
    global_contract = selection_output_contract(GLOBAL_SPEC.selection)
    market_contract = selection_output_contract(TW_EQUITY_SPEC.selection)
    assert "0 to 5 objects" in global_contract["selections"]
    assert "2 distinct markets" in global_contract["selections"]
    assert "0 to 8 objects" in market_contract["selections"]
    assert "distinct markets" not in market_contract["selections"]
    assert "taiwan" in market_contract["market"]


def test_feed_sources_are_tagged_per_market() -> None:
    by_market: dict[str, set[str]] = {}
    for source in FEED_SOURCES:
        for market in source.markets:
            by_market.setdefault(market, set()).add(source.url)
    assert by_market["tw_equity"] == {"https://news.cnyes.com/news/cat/tw_stock"}
    assert "https://news.cnyes.com/news/cat/us_stock" in by_market["us_equity"]
    assert "https://www.cnbc.com/id/10000664/device/rss/rss.html" in by_market["us_equity"]
    assert "https://feeds.bbci.co.uk/news/business/rss.xml" not in by_market["us_equity"]
    assert all(market in EDITION_SPECS for market in by_market)


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
    assert requested == ["https://news.cnyes.com/news/cat/tw_stock"]
