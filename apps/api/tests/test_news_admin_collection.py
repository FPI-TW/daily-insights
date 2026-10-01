import dataclasses
import hashlib

from daily_insights_api.modules.news.admin import (
    FEED_SOURCE_BY_KEY,
    OTHER_STAGE_GROUP,
    STAGE_ORDER,
    _public_feed_url,
)
from daily_insights_api.modules.news.feeds import FEED_SOURCES, FeedSource


def _source(url_fragment: str) -> FeedSource:
    return next(source for source in FEED_SOURCES if url_fragment in source.url)


def test_poll_states_join_the_registry_by_feed_url_sha256() -> None:
    assert len(FEED_SOURCE_BY_KEY) == len(FEED_SOURCES)
    for source in FEED_SOURCES:
        assert FEED_SOURCE_BY_KEY[hashlib.sha256(source.url.encode()).hexdigest()] is source


def test_registry_feed_url_drops_credentials_but_keeps_its_section_query() -> None:
    guardian = _source("content.guardianapis.com/search?section=business")
    assert guardian.api_key_setting is not None
    keyed = f"{guardian.url}&{guardian.api_key_param}=secret-key&token=abc"

    public = _public_feed_url(keyed, guardian)

    assert "secret-key" not in public and "abc" not in public
    assert guardian.api_key_param not in public
    assert public.startswith("https://content.guardianapis.com/search?")
    assert "section=business" in public


def test_registry_feed_url_drops_its_cache_buster() -> None:
    source = dataclasses.replace(FEED_SOURCES[0], cache_buster_param="_cb")

    assert _public_feed_url(f"{source.url}?_cb=123", source) == source.url


def test_unregistered_feed_url_keeps_only_host_and_path() -> None:
    public = _public_feed_url(
        "https://user:password@old.example:8443/feed.xml?apikey=secret&section=x#top", None
    )

    assert public == "https://old.example:8443/feed.xml"


def test_screened_out_candidates_sort_after_every_other_stage() -> None:
    others = [order for stage, order in STAGE_ORDER.items() if stage != "screened_out"]
    assert STAGE_ORDER["screened_out"] > max(*others, OTHER_STAGE_GROUP)
