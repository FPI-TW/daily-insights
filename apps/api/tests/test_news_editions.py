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
        assert spec.target_items == spec.selection.max_items
        assert spec.max_candidates >= spec.target_items
        assert spec.max_per_source >= 1
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
            by_market.setdefault(market, set()).add(source.hostname)
    # Taiwanese media feed only the Taiwan edition; cnyes' international desk
    # and the English sources feed the US edition alongside the global digest.
    assert "news.cnyes.com" in by_market["tw_equity"]
    assert "money.udn.com" in by_market["tw_equity"]
    assert "www.wsj.com" not in by_market["tw_equity"]
    assert {"news.cnyes.com", "www.wsj.com", "www.globenewswire.com"} <= by_market["us_equity"]
    assert "www.sec.gov" in by_market["us_equity"] and "www.sec.gov" not in by_market["global"]
    assert {"www.cls.cn", "www.etnet.com.hk", "www.hankyung.com"} <= by_market["global"]
    assert by_market["cn_equity"] <= by_market["global"]
    assert by_market["hk_equity"] <= by_market["global"]
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
    # The global digest accepts every market tag.
    assert filter_selection_markets(selection, GLOBAL_SPEC.selection) == (selection, ())


def test_output_contract_restricts_market_tags_for_market_editions() -> None:
    assert selection_output_contract(TW_EQUITY_SPEC.selection)["market"] == ["taiwan"]
    assert selection_output_contract(US_EQUITY_SPEC.selection)["market"] == ["us"]
    assert "global" in selection_output_contract(GLOBAL_SPEC.selection)["market"]
    assert GLOBAL_SPEC.selection.market_focus is not None
    assert "macro" in GLOBAL_SPEC.selection.market_focus
