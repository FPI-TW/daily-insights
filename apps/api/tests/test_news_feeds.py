from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from defusedxml import EntitiesForbidden

import daily_insights_api.modules.news.feeds as feeds
from daily_insights_api.modules.news.extraction import configured_hostnames
from daily_insights_api.modules.news.feeds import (
    FEED_KINDS,
    FEED_SOURCES,
    POLL_GROUPS,
    FeedSource,
    discover_feed_candidates,
    effective_hostnames,
    filter_window,
    newest_seen_at,
    normalize_article_url,
    parse_rss,
    parse_rss_entries,
    registry_hostnames,
)

FIXTURES = Path(__file__).parent / "fixtures" / "news"
NOW = datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
START = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
BBC = FeedSource(
    "www.bbc.com",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "rss",
    r"^https://www\.bbc\.com/news/articles/[a-z0-9]+$",
    host_rewrites=(("www.bbc.co.uk", "www.bbc.com"),),
    display_name="BBC Business",
)
AP = FeedSource(
    "apnews.com",
    "https://apnews.com/business.rss",
    "rss",
    r"^https://apnews\.com/article/[a-z0-9-]+$",
    display_name="AP",
)
CNYES = FeedSource(
    "news.cnyes.com",
    "https://news.cnyes.com/rss/v1/news/category/headline",
    "rss",
    r"^https://news\.cnyes\.com/news/id/\d+$",
)
ETNET = next(source for source in FEED_SOURCES if source.hostname == "www.etnet.com.hk")
GVM = next(source for source in FEED_SOURCES if source.hostname == "www.gvm.com.tw")

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

STALE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Zombie</title>
<item><title>Ancient one</title><link>https://www.bbc.co.uk/news/articles/z1</link>
  <pubDate>Mon, 01 Jun 2026 01:00:00 GMT</pubDate></item>
<item><title>Ancient two</title><link>https://www.bbc.co.uk/news/articles/z2</link>
  <pubDate>Sun, 31 May 2026 01:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_rss_parsing_rewrites_host_and_keeps_dated_items_for_the_window_filter() -> None:
    parsed = parse_rss(RSS, BBC)
    # Parsing keeps every dated item; the window is applied separately so
    # freshness can be judged on the whole feed first.
    assert [str(item.url) for item in parsed] == [
        "https://www.bbc.com/news/articles/c1",
        "https://www.bbc.com/news/articles/c2",
        "https://www.bbc.com/news/articles/c1",
    ]
    assert newest_seen_at(parsed) == datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
    result = filter_window(parsed, START, NOW)
    assert [str(item.url) for item in result] == [
        "https://www.bbc.com/news/articles/c1",
        "https://www.bbc.com/news/articles/c1",
    ]
    assert result[0].source_name == "BBC Business" and result[0].headline == "Fresh story"


def test_rss_rejects_entity_expansion() -> None:
    payload = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">
    <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
    <rss><channel><item><title>&lol2;</title><link>https://www.bbc.com/news/articles/c1</link>
    </item></channel></rss>"""
    with pytest.raises(EntitiesForbidden):
        parse_rss(payload, BBC)


def test_atom_entries_use_link_href_and_updated() -> None:
    sec = FeedSource(
        "www.example-news.com",
        "https://www.example-news.com/atom",
        "rss",
        r"^https://www\.example-news\.com/Archives/edgar/data/\d+/\d+/[0-9-]+-index\.htm$",
    )
    result = parse_rss((FIXTURES / "atom_sec.xml").read_bytes(), sec)

    assert [(c.headline, c.seen_at) for c in result] == [
        ("8-K - RELIABILITY INC (0000034285) (Filer)", datetime(2026, 9, 2, 21, 30, 10, tzinfo=UTC))
    ]


def test_naive_timestamps_need_a_declared_zone() -> None:
    payload = b"""<rss><channel>
    <item><title>Naive</title><link>https://www.gvm.com.tw/article/1</link>
      <pubDate>Wed, 02 Sep 2026 21:18:00</pubDate></item></channel></rss>"""

    [dated] = parse_rss(payload, GVM)
    assert dated.seen_at == datetime(2026, 9, 2, 13, 18, tzinfo=UTC)

    undeclared = FeedSource("www.gvm.com.tw", GVM.url, "rss", GVM.link_pattern)
    [undated] = parse_rss(payload, undeclared)
    assert undated.seen_at is None


def test_rss_full_falls_back_to_a_long_description_body() -> None:
    mirror = FeedSource(
        "m.jiemian.com",
        "https://feedx.net/rss/jiemian.xml",
        "rss_full",
        r"^https://m\.jiemian\.com/article/\d+\.html$",
    )
    long_text = "界面新聞報導內容。" * 40
    payload = f"""<rss><channel><item><title>全文</title>
    <link>https://m.jiemian.com/article/1.html</link>
    <description><![CDATA[<div>2026.09.03</div><p>{long_text}</p>]]></description>
    </item></channel></rss>""".encode()

    [(candidate, body)] = parse_rss_entries(payload, mirror)

    assert candidate.headline == "全文"
    assert body is not None and body.startswith("2026.09.03") and long_text[:50] in body


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


def test_normalize_keeps_the_query_string_only_when_the_source_asks() -> None:
    href = (
        "http://www.etnet.com.hk/www/tc/news/home_categorized_news_detail.php?newsid=ETN360903320"
    )
    assert normalize_article_url(href, ETNET) == (
        "https://www.etnet.com.hk/www/tc/news/home_categorized_news_detail.php?newsid=ETN360903320"
    )
    assert (
        normalize_article_url("https://www.etnet.com.hk/www/tc/news/rss.php?section=editor", ETNET)
        is None
    )


async def test_discovery_isolates_feed_failures_and_only_reads_allowlisted_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def robots(_: httpx.AsyncClient, __: str, ___: frozenset[str]) -> bool:
        return True

    monkeypatch.setattr(feeds, "robots_allowed", robots)
    monkeypatch.setattr(feeds, "FEED_SOURCES", (BBC, AP, CNYES))
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "feeds.bbci.co.uk":
            return httpx.Response(200, content=RSS, headers={"content-type": "text/xml"})
        return httpx.Response(500)

    allowed = configured_hostnames("www.bbc.com,apnews.com,www.reuters.com")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_feed_candidates(client, allowed, NOW)

    # cnyes is not allowlisted, so its feed is never requested; AP's failure
    # does not affect BBC's results.
    assert sorted(requested) == [
        "https://apnews.com/business.rss",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
    ]
    assert [str(item.url) for item in result] == ["https://www.bbc.com/news/articles/c1"]


async def test_discovery_respects_robots_and_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def robots(_: httpx.AsyncClient, url: str, ___: frozenset[str]) -> bool:
        return "bbci" not in url

    monkeypatch.setattr(feeds, "robots_allowed", robots)
    monkeypatch.setattr(feeds, "FEED_SOURCES", (BBC, AP))
    monkeypatch.setattr(feeds, "MAX_FEED_BYTES", 64)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=RSS)

    allowed = configured_hostnames("www.bbc.com,apnews.com")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await discover_feed_candidates(client, allowed, NOW) == []


def test_registry_is_internally_consistent() -> None:
    seen_urls: set[tuple[str, str]] = set()
    for source in FEED_SOURCES:
        assert source.url.startswith("https://")
        assert source.kind in FEED_KINDS
        assert source.poll_group in POLL_GROUPS
        assert source.link_pattern, source.url
        assert source.display_name, source.url
        assert source.markets, source.url
        assert (source.kind == "json_list") == (source.mapping is not None), source.url
        if source.mapping is not None:
            assert source.mapping.url_field or (
                source.mapping.url_template and source.mapping.id_field
            ), source.url
            assert source.mapping.time_format in {"unix_s", "unix_ms", "iso", "datetime_str"}
        if source.provides_full_text:
            assert source.kind in {"rss_full", "json_list"}, source.url
        assert (source.hostname, source.url) not in seen_urls, source.url
        seen_urls.add((source.hostname, source.url))
    # This legacy endpoint returns a freshly generated channel containing only
    # stale articles, so it must not silently return to production discovery.
    assert "https://finance.yahoo.com/news/rssindex" not in {source.url for source in FEED_SOURCES}
    # Publishers that block crawlers stay out of the registry.
    assert not registry_hostnames() & {
        "www.bbc.com",
        "apnews.com",
        "www.reuters.com",
        "www.wsj.com",
        "www.forbes.com",
        "www.investing.com",
        "www.marketwatch.com",
    }


def test_effective_hostnames_adds_and_blocks_registry_hosts() -> None:
    assert effective_hostnames() == registry_hostnames()
    assert "news.cnyes.com" in effective_hostnames()
    extended = effective_hostnames("www.example-news.com, extra.example")
    assert extended == registry_hostnames() | {"www.example-news.com", "extra.example"}
    blocked = effective_hostnames(blocked="news.cnyes.com")
    assert "news.cnyes.com" not in blocked and blocked < registry_hostnames()
    with pytest.raises(ValueError, match="exact hostnames"):
        effective_hostnames("not a host")


async def test_discovery_reports_stale_feeds_but_fresh_ones_as_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def robots(_: httpx.AsyncClient, __: str, ___: frozenset[str]) -> bool:
        return True

    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(feeds, "robots_allowed", robots)
    monkeypatch.setattr(feeds, "emit_event", lambda name, **fields: events.append((name, fields)))
    monkeypatch.setattr(
        feeds,
        "FEED_SOURCES",
        (
            FeedSource(
                "www.bbc.com",
                "https://stale.example/rss.xml",
                "rss",
                r"^https://www\.bbc\.com/news/articles/[a-z0-9]+$",
                host_rewrites=(("www.bbc.co.uk", "www.bbc.com"),),
                max_age_hours=48,
            ),
            FeedSource(
                "www.bbc.com",
                "https://fresh.example/rss.xml",
                "rss",
                r"^https://www\.bbc\.com/news/articles/[a-z0-9]+$",
                host_rewrites=(("www.bbc.co.uk", "www.bbc.com"),),
            ),
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = STALE_RSS if request.url.host == "stale.example" else RSS
        return httpx.Response(200, content=payload, headers={"content-type": "text/xml"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_feed_candidates(client, configured_hostnames("www.bbc.com"), NOW)

    # The stale feed's items are outside the window, so only the fresh story survives...
    assert [str(item.url) for item in result] == ["https://www.bbc.com/news/articles/c1"]
    # ...but the stale feed is reported with its age rather than silently looking empty.
    stale = [fields for name, fields in events if name == "news.feed.stale"]
    assert len(stale) == 1
    assert stale[0]["feed"] == "https://stale.example/rss.xml"
    age_hours = stale[0]["age_hours"]
    assert isinstance(age_hours, float) and age_hours > 48
    assert stale[0]["max_age_hours"] == 48
    ok = {str(fields["feed"]): fields for name, fields in events if name == "news.feed.ok"}
    # Two identical "Fresh story" items count before dedupe; discovery dedupes at the end.
    assert ok["https://fresh.example/rss.xml"]["count"] == 2
    assert ok["https://fresh.example/rss.xml"]["newest_age_minutes"] == 120
    assert ok["https://stale.example/rss.xml"]["count"] == 0


async def test_discovery_drops_foreign_language_items_from_flagged_feeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def robots(_: httpx.AsyncClient, __: str, ___: frozenset[str]) -> bool:
        return True

    events: list[tuple[str, dict[str, object]]] = []
    wire = FeedSource(
        "www.example-news.com",
        "https://www.example-news.com/wire.rss",
        "rss",
        r"^https://www\.example-news\.com/news-releases/[a-z0-9-]+\.html$",
        language_filter=True,
    )
    monkeypatch.setattr(feeds, "robots_allowed", robots)
    monkeypatch.setattr(feeds, "emit_event", lambda name, **fields: events.append((name, fields)))
    monkeypatch.setattr(feeds, "FEED_SOURCES", (wire,))

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=(FIXTURES / "rss_mixed_language.xml").read_bytes())

    allowed = configured_hostnames("www.example-news.com")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_feed_candidates(client, allowed, NOW)

    slugs = [str(item.url).rsplit("/", 1)[-1] for item in result]
    # English and Chinese stay; the Slovak release goes; bare tickers are
    # undetectable (digits only) and therefore kept.
    assert "acme-record-quarter.html" in slugs and "tsmc-q2.html" in slugs
    assert "acme-rekordny-stvrtrok.html" not in slugs
    assert "tickers.html" in slugs
    [ok] = [fields for name, fields in events if name == "news.feed.ok"]
    assert ok["dropped_language"] == 1
