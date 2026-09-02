from datetime import UTC, datetime

import httpx
import pytest

import daily_insights_api.modules.news.feeds as feeds
from daily_insights_api.modules.news.feeds import (
    FEED_SOURCES,
    FeedSource,
    discover_feed_candidates,
    normalize_article_url,
    parse_listing,
    parse_rss,
)
from daily_insights_api.modules.news.sources import configured_hostnames

NOW = datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
START = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
CNBC = next(source for source in FEED_SOURCES if source.hostname == "www.cnbc.com")
BBC = next(source for source in FEED_SOURCES if source.hostname == "www.bbc.com")
AP = next(source for source in FEED_SOURCES if source.hostname == "apnews.com")
CNYES = FeedSource(
    "news.cnyes.com",
    "https://news.cnyes.com/news/cat/headline",
    "listing",
    r"^https://news\.cnyes\.com/news/id/\d+$",
)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Business</title>
<item><title>Fresh story</title><link>https://www.bbc.co.uk/news/articles/c1?at_medium=RSS</link>
  <pubDate>Wed, 02 Sep 2026 01:00:00 GMT</pubDate></item>
<item><title>Stale story</title><link>https://www.bbc.co.uk/news/articles/c2</link>
  <pubDate>Mon, 31 Aug 2026 01:00:00 GMT</pubDate></item>
<item><title>Other host</title><link>https://www.bbc.co.uk.evil.example/news/articles/c3</link>
  <pubDate>Wed, 02 Sep 2026 01:00:00 GMT</pubDate></item>
<item><title>Video page</title><link>https://www.bbc.co.uk/news/videos/v1</link>
  <pubDate>Wed, 02 Sep 2026 01:00:00 GMT</pubDate></item>
<item><title>Fresh story</title><link>https://www.bbc.co.uk/news/articles/c1</link>
  <pubDate>Wed, 02 Sep 2026 01:00:00 GMT</pubDate></item>
</channel></rss>"""

LISTING = """
<html><body>
<nav><a href="/business">Business</a></nav>
<a href="/article/nepal-flash-floods-death-toll"><span>Mass burials in Nepal as</span>
  <span>death toll exceeds 1,000</span></a>
<a href="https://apnews.com/article/mideast-iran-israel?utm_source=x">US and Iran trade strikes</a>
<a href="https://apnews.com/article/mideast-iran-israel">short</a>
<a href="https://apnews.com/hub/politics">Politics</a>
<a href="http://apnews.com/article/only-slug-headline-here"></a>
<a href="https://apnews.com/article/x"></a>
<a href="https://other.example/article/not-allowed">Not allowed</a>
</body></html>
"""


def test_rss_parsing_rewrites_host_filters_window_and_deduplicates() -> None:
    result = parse_rss(RSS, BBC, START, NOW)
    assert [(item.headline, str(item.url)) for item in result] == [
        ("Fresh story", "https://www.bbc.com/news/articles/c1"),
        ("Fresh story", "https://www.bbc.com/news/articles/c1"),
    ]
    assert result[0].hostname == "www.bbc.com"
    assert result[0].source_name == "BBC Business"
    assert result[0].seen_at == datetime(2026, 9, 2, 1, 0, tzinfo=UTC)


def test_rss_rejects_entity_expansion() -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        b"<rss><channel><item><title>&lol2;</title></item></channel></rss>"
    )
    with pytest.raises(Exception, match=r"(?i)entit"):
        parse_rss(bomb, CNBC, START, NOW)


def test_listing_parsing_uses_anchor_text_slug_fallback_and_pattern() -> None:
    result = parse_listing(LISTING, AP)
    assert [(item.headline, str(item.url)) for item in result] == [
        (
            "Mass burials in Nepal as death toll exceeds 1,000",
            "https://apnews.com/article/nepal-flash-floods-death-toll",
        ),
        ("US and Iran trade strikes", "https://apnews.com/article/mideast-iran-israel"),
        ("only slug headline here", "https://apnews.com/article/only-slug-headline-here"),
    ]
    assert all(item.seen_at is None for item in result)


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("https://news.cnyes.com/news/id/6594974", "https://news.cnyes.com/news/id/6594974"),
        ("/news/id/6594975?exp=a#top", "https://news.cnyes.com/news/id/6594975"),
        ("http://news.cnyes.com/news/id/1", "https://news.cnyes.com/news/id/1"),
        ("https://news.cnyes.com:8443/news/id/1", None),
        ("https://user:pw@news.cnyes.com/news/id/1", "https://news.cnyes.com/news/id/1"),
        ("https://news.cnyes.com/news/cat/tw_stock", None),
        ("https://evil.example/news/id/1", None),
    ],
)
def test_normalize_article_url(href: str, expected: str | None) -> None:
    assert normalize_article_url(href, CNYES) == expected


async def test_discovery_isolates_feed_failures_and_only_reads_allowlisted_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def robots(_: httpx.AsyncClient, __: str, ___: frozenset[str]) -> bool:
        return True

    monkeypatch.setattr(feeds, "robots_allowed", robots)
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "feeds.bbci.co.uk":
            return httpx.Response(200, content=RSS, headers={"content-type": "text/xml"})
        if request.url.host == "apnews.com":
            return httpx.Response(500)
        return httpx.Response(200, text=LISTING, headers={"content-type": "text/html"})

    allowed = configured_hostnames("www.bbc.com,apnews.com,www.reuters.com")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_feed_candidates(client, allowed, NOW)

    # Reuters has no feed source and CNBC/cnyes/eastmoney are not allowlisted.
    assert sorted(requested) == [
        "https://apnews.com/business",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
    ]
    assert [str(item.url) for item in result] == ["https://www.bbc.com/news/articles/c1"]


async def test_discovery_respects_robots_and_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def robots(_: httpx.AsyncClient, url: str, ___: frozenset[str]) -> bool:
        return "bbci" not in url

    monkeypatch.setattr(feeds, "robots_allowed", robots)
    monkeypatch.setattr(feeds, "MAX_FEED_BYTES", 64)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LISTING)

    allowed = configured_hostnames("www.bbc.com,apnews.com")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await discover_feed_candidates(client, allowed, NOW) == []


def test_feed_sources_only_reference_allowlisted_article_hosts() -> None:
    default_hosts = configured_hostnames(
        "www.reuters.com,apnews.com,www.bbc.com,www.cnbc.com,news.cnyes.com,finance.eastmoney.com"
    )
    for source in FEED_SOURCES:
        assert isinstance(source, FeedSource)
        assert source.hostname in default_hosts
        assert source.url.startswith("https://")
        assert source.kind in {"rss", "listing", "cnyes_json"}
        assert source.kind == "rss" or source.link_pattern


def test_cnyes_json_parsing_maps_ids_titles_and_publish_times() -> None:
    from daily_insights_api.modules.news.feeds import parse_cnyes_json

    source = next(s for s in FEED_SOURCES if "tw_stock" in s.url)
    payload = b"""{"items":{"data":[
      {"newsId":6594061,"title":"  \u3008SEMICON\u3009 \u7cbe\u6e2c  \u7522\u80fd",
       "publishAt":1788316800},
      {"newsId":6594062,"title":"Stale","publishAt":1756500000},
      {"newsId":"bad","title":"Bad id","publishAt":1788316800},
      {"newsId":6594063,"title":"","publishAt":1788316800},
      {"newsId":6594064,"title":"No time"}
    ]},"statusCode":200}"""
    result = parse_cnyes_json(payload, source, START, NOW)
    assert [(str(item.url), item.headline) for item in result] == [
        ("https://news.cnyes.com/news/id/6594061", "\u3008SEMICON\u3009 \u7cbe\u6e2c \u7522\u80fd"),
        ("https://news.cnyes.com/news/id/6594064", "No time"),
    ]
    assert result[0].seen_at == datetime(2026, 9, 2, 2, 40, tzinfo=UTC)
    assert result[0].hostname == "news.cnyes.com" and result[0].source_name == "\u9245\u4ea8"
    with pytest.raises(ValueError, match=r"items\.data"):
        parse_cnyes_json(b'{"items": []}', source, START, NOW)
