"""Feed adapters moved from news/feeds.py, driven by newsroom source rows."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from defusedxml import EntitiesForbidden
from pydantic import SecretStr

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.ingestion.feeds import (
    GUARDIAN_MAPPING,
    JsonListMapping,
    MissingCredentialError,
    SourceConfig,
    build_request,
    entry_language,
    keeps_language,
    normalize_article_url,
    normalize_manual_url,
    parse_feed,
    parse_json_list,
    parse_news_sitemap,
    parse_xml_entries,
    url_hash,
)

FIXTURES = Path(__file__).parent / "fixtures" / "newsroom"
HOST = "www.example-news.com"
KEPT = frozenset({"en", "zh-cn", "zh-tw", "ja", "ko"})


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def source(kind: str, url: str = f"https://{HOST}/feed", **overrides: Any) -> SourceConfig:
    values: dict[str, Any] = {
        "key": "example",
        "kind": kind,
        "url": url,
        "hostname": HOST,
        "link_pattern": r"^https://[^/]+/.+",
    }
    values.update(overrides)
    return SourceConfig(**values)


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, environment="test", **overrides)


CNYES = source(
    "rss",
    url="https://news.cnyes.com/rss/v1/news/category/headline",
    hostname="news.cnyes.com",
    link_pattern=r"^https://news\.cnyes\.com/news/id/\d+$",
)


# --- XML feeds ----------------------------------------------------------------


def test_rss_keeps_host_matching_story_links_and_rewrites_hosts() -> None:
    bbc = source(
        "rss",
        url="https://feeds.bbci.co.uk/news/business/rss.xml",
        hostname="www.bbc.com",
        link_pattern=r"^https://www\.bbc\.com/news/articles/[a-z0-9]+$",
        options={"host_rewrites": [["www.bbc.co.uk", "www.bbc.com"]]},
    )
    payload = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Fresh  story</title><link>https://www.bbc.co.uk/news/articles/c1?at_medium=RSS</link>
  <pubDate>Wed, 02 Sep 2026 01:00:00 GMT</pubDate>
  <description><![CDATA[<p>Short <b>summary</b></p>]]></description></item>
<item><title>Other host</title><link>https://www.bbc.co.uk.evil.example/news/articles/c3</link></item>
<item><title>Video page</title><link>https://www.bbc.co.uk/news/videos/v1</link></item>
</channel></rss>"""

    [entry] = parse_xml_entries(payload, bbc)

    assert entry.url == "https://www.bbc.com/news/articles/c1"
    assert entry.title == "Fresh story"
    assert entry.published_at == datetime(2026, 9, 2, 1, 0, tzinfo=UTC)
    assert entry.summary == "Short summary"
    assert entry.body is None


def test_rss_rejects_entity_expansion() -> None:
    payload = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">
    <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
    <rss><channel><item><title>&lol2;</title><link>https://news.cnyes.com/news/id/1</link>
    </item></channel></rss>"""
    with pytest.raises(EntitiesForbidden):
        parse_xml_entries(payload, CNYES)


def test_atom_entries_use_link_href_and_updated() -> None:
    sec = source(
        "atom",
        link_pattern=(
            r"^https://www\.example-news\.com/Archives/edgar/data/\d+/\d+/[0-9-]+-index\.htm$"
        ),
    )

    entries = parse_feed(sec, fixture("atom_sec.xml"))

    assert [(e.title, e.published_at) for e in entries] == [
        ("8-K - RELIABILITY INC (0000034285) (Filer)", datetime(2026, 9, 2, 21, 30, 10, tzinfo=UTC))
    ]


def test_rdf_items_use_dc_date() -> None:
    entries = parse_feed(source("rdf"), fixture("rss_rdf.xml"))

    assert [(e.url, e.published_at) for e in entries] == [
        (f"https://{HOST}/news/id/20", datetime(2026, 9, 2, 2, 30, tzinfo=UTC)),
        (f"https://{HOST}/news/id/21", None),
    ]


def test_naive_timestamps_need_a_declared_zone() -> None:
    payload = b"""<rss><channel>
    <item><title>Naive</title><link>https://www.example-news.com/article/1</link>
      <pubDate>Wed, 02 Sep 2026 21:18:00</pubDate></item></channel></rss>"""

    [dated] = parse_feed(source("rss", options={"naive_time_zone": "Asia/Taipei"}), payload)
    [undated] = parse_feed(source("rss"), payload)

    assert dated.published_at == datetime(2026, 9, 2, 13, 18, tzinfo=UTC)
    assert undated.published_at is None


def test_rss_full_keeps_only_long_content_encoded_bodies() -> None:
    entries = parse_feed(source("rss_full", full_text_in_feed=True), fixture("rss_full_ok.xml"))

    assert [e.title for e in entries] == ["Full article", "Teaser article", "No encoded body"]
    body = entries[0].body
    assert body is not None and body.startswith("This paragraph is long")
    assert "alert(1)" not in body and body.endswith("Second paragraph.")
    assert entries[1].body is None and entries[2].body is None


def test_rss_full_falls_back_to_a_long_description_body() -> None:
    long_text = "界面新聞報導內容。" * 40
    payload = f"""<rss><channel><item><title>全文</title>
    <link>https://www.example-news.com/article/1.html</link>
    <description><![CDATA[<div>2026.09.03</div><p>{long_text}</p>]]></description>
    </item></channel></rss>""".encode()

    [entry] = parse_feed(source("rss_full"), payload)

    assert entry.body is not None and entry.body.startswith("2026.09.03")


def test_plain_rss_never_carries_a_body() -> None:
    entries = parse_feed(source("rss"), fixture("rss_full_ok.xml"))

    assert all(entry.body is None for entry in entries)


def test_rss_skips_items_missing_title_or_link_but_keeps_undated_ones() -> None:
    entries = parse_feed(source("rss_full"), fixture("rss_full_missing_fields.xml"))

    assert [(e.title, e.published_at) for e in entries] == [("Undated but kept", None)]


# --- news sitemap -------------------------------------------------------------


def test_news_sitemap_maps_loc_title_and_publication_date() -> None:
    entries = parse_news_sitemap(fixture("news_sitemap_ok.xml"), source("news_sitemap"))

    assert [(e.url, e.title) for e in entries] == [
        (f"https://{HOST}/markets/fed-holds-rates", "Fed holds rates steady"),
        (f"https://{HOST}/markets/tsmc-record", "TSMC posts record quarter"),
    ]
    assert entries[0].published_at == datetime(2026, 9, 2, 2, 30, tzinfo=UTC)
    assert entries[1].published_at == datetime(2026, 9, 2, 1, 15, tzinfo=UTC)


def test_news_sitemap_drops_incomplete_entries_and_keeps_bad_dates_undated() -> None:
    missing = parse_news_sitemap(fixture("news_sitemap_missing_fields.xml"), source("news_sitemap"))
    bad_time = parse_news_sitemap(fixture("news_sitemap_bad_time.xml"), source("news_sitemap"))

    assert [e.title for e in missing] == ["Complete entry"]
    assert [(e.title, e.published_at) for e in bad_time] == [
        ("Undated entry", None),
        ("Naive timestamp entry", None),
    ]


# --- JSON lists and the Guardian API ------------------------------------------


def test_guardian_api_maps_nested_fields_and_only_keeps_long_bodies() -> None:
    guardian = source("guardian_api", full_text_in_feed=True)

    entries = parse_feed(guardian, fixture("json_list_ok.json"))

    assert [(e.title, e.published_at) for e in entries] == [
        ("Fed holds rates", datetime(2026, 9, 2, 2, 30, tzinfo=UTC)),
        ("Teaser only", datetime(2026, 9, 2, 1, 0, tzinfo=UTC)),
    ]
    assert entries[0].body is not None and entries[0].body.startswith("This paragraph is long")
    assert entries[1].body is None


def test_json_list_skips_incomplete_items_and_unparseable_times() -> None:
    guardian = source("guardian_api", full_text_in_feed=True)

    missing = parse_feed(guardian, fixture("json_list_missing_fields.json"))
    bad_time = parse_feed(guardian, fixture("json_list_bad_time.json"))

    assert [e.title for e in missing] == ["Has everything", "No fields block"]
    assert [(e.title, e.published_at) for e in bad_time] == [
        ("Bad time", None),
        ("Null time", None),
    ]


def test_json_list_mapping_comes_from_options() -> None:
    jin10 = source(
        "json_list",
        url="https://www.jin10.com/flash_newest.js",
        hostname="www.jin10.com",
        options={
            "mapping": {
                "items_path": [],
                "id_field": "id",
                "url_template": "https://www.jin10.com/flash/{id}",
                "title_field": "data.content",
                "time_field": "time",
                "time_format": "datetime_str",
                "js_prefix": True,
            }
        },
    )

    entries = parse_feed(jin10, fixture("jin10_flash.js"))

    assert [e.url for e in entries] == [
        "https://www.jin10.com/flash/20260902103000123456",
        "https://www.jin10.com/flash/20260902102000111111",
    ]
    assert entries[0].published_at == datetime(2026, 9, 2, 2, 30, tzinfo=UTC)


def test_json_list_rejects_missing_mapping_array_or_unknown_keys() -> None:
    with pytest.raises(ValueError, match="no array"):
        parse_json_list(b'{"response": {}}', source("guardian_api"), GUARDIAN_MAPPING)
    with pytest.raises(ValueError, match="no mapping"):
        parse_feed(source("json_list"), b"[]")
    with pytest.raises(ValueError, match="unknown json_list mapping keys"):
        parse_feed(source("json_list", options={"mapping": {"evil": 1}}), b"[]")


@pytest.mark.parametrize(
    ("time_format", "value", "expected"),
    [
        ("unix_s", 1788316200, datetime(2026, 9, 2, 2, 30, tzinfo=UTC)),
        ("unix_ms", 1788316200000, datetime(2026, 9, 2, 2, 30, tzinfo=UTC)),
        ("unix_ms", "garbage", None),
        ("iso", "2026-09-02T02:30:00Z", datetime(2026, 9, 2, 2, 30, tzinfo=UTC)),
        ("datetime_str", "2026-09-02 10:30:00", datetime(2026, 9, 2, 2, 30, tzinfo=UTC)),
        ("datetime_str", "2026/09/02", None),
    ],
)
def test_json_list_time_formats(time_format: str, value: Any, expected: datetime | None) -> None:
    mapping = JsonListMapping(
        items_path=("items",),
        url_field="url",
        title_field="title",
        time_field="time",
        time_format=time_format,
    )
    payload = json.dumps(
        {"items": [{"url": f"https://{HOST}/a", "title": "A", "time": value}]}
    ).encode()

    [entry] = parse_json_list(payload, source("json_list"), mapping)

    assert entry.published_at == expected


# --- URLs ---------------------------------------------------------------------


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
    etnet = source(
        "rss",
        url="https://www.etnet.com.hk/www/tc/news/rss.php?section=editor",
        hostname="www.etnet.com.hk",
        link_pattern=r"^https://www\.etnet\.com\.hk/www/tc/news/detail\.php\?newsid=ETN\d+$",
        options={"keep_query": True},
    )
    href = "http://www.etnet.com.hk/www/tc/news/detail.php?newsid=ETN360903320"

    assert normalize_article_url(href, etnet) == (
        "https://www.etnet.com.hk/www/tc/news/detail.php?newsid=ETN360903320"
    )
    assert normalize_article_url(href, CNYES) is None


def test_manual_urls_normalise_host_and_drop_fragment_and_port() -> None:
    url = normalize_manual_url(" https://WWW.Example.com:443/a/b?id=1#frag ")

    assert url == "https://www.example.com/a/b?id=1"
    assert url_hash(url) == url_hash("https://www.example.com/a/b?id=1")
    assert len(url_hash(url)) == 64


# --- request shaping ----------------------------------------------------------


def test_guardian_requests_need_the_key_and_the_guardian_api_host() -> None:
    guardian = source(
        "guardian_api", url="https://content.guardianapis.com/search?section=business"
    )

    with pytest.raises(MissingCredentialError, match="guardian_api_key_missing"):
        build_request(guardian, settings())
    with pytest.raises(MissingCredentialError):
        build_request(guardian, settings(guardian_api_key=SecretStr("  ")))
    url, headers = build_request(guardian, settings(guardian_api_key=SecretStr("abc")))
    assert url == "https://content.guardianapis.com/search?section=business&api-key=abc"
    assert headers == {}
    elsewhere = source("guardian_api", url="https://evil.example/search")
    with pytest.raises(ValueError, match="Guardian Content API host"):
        build_request(elsewhere, settings(guardian_api_key=SecretStr("abc")))


def test_sec_requests_need_a_contact_email_in_the_user_agent() -> None:
    sec = source("atom", options={"requires_contact_email": True})

    with pytest.raises(MissingCredentialError, match="sec_contact_email_missing"):
        build_request(sec, settings())
    with pytest.raises(MissingCredentialError):
        build_request(sec, settings(sec_contact_email="nobody"))
    _, headers = build_request(sec, settings(sec_contact_email="ops@example.com"))
    assert headers == {"User-Agent": "DailyInsightsNewsBot/1.0 (FPI-TW; ops@example.com)"}


def test_cache_buster_changes_per_request() -> None:
    listing = source(
        "json_list", url=f"https://{HOST}/list?type=1", options={"cache_buster_param": "r"}
    )

    urls = {build_request(listing, settings())[0] for _ in range(5)}

    assert len(urls) > 1 and all("&r=" in url for url in urls)


def test_language_filter_drops_only_confident_foreign_languages() -> None:
    entries = parse_feed(source("rss"), fixture("rss_mixed_language.xml"))

    kept = [
        entry.url.rsplit("/", 1)[-1]
        for entry in entries
        if keeps_language(entry_language(entry), KEPT)
    ]

    assert kept == ["acme-record-quarter.html", "tsmc-q2.html", "tickers.html"]
    assert all(keeps_language(entry_language(entry), None) for entry in entries)
