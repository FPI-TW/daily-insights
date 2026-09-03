"""Fixture-driven tests for the sitemap, JSON list and full-text RSS adapters."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.news import feeds
from daily_insights_api.modules.news.extraction import configured_hostnames
from daily_insights_api.modules.news.feeds import (
    FeedSource,
    JsonListMapping,
    discover_feed_candidates,
    parse_json_list,
    parse_news_sitemap,
    parse_rss,
    parse_rss_entries,
    request_headers,
    request_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "news"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
HOST = "www.example-news.com"
ALLOWED = configured_hostnames(f"{HOST},finance.eastmoney.com,www.jin10.com")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def source(kind: str, url: str = f"https://{HOST}/feed", **overrides: Any) -> FeedSource:
    values: dict[str, Any] = {
        "hostname": HOST,
        "url": url,
        "kind": kind,
        "link_pattern": r"^https://[^/]+/.+",
        "markets": ("global",),
    }
    values.update(overrides)
    return FeedSource(**values)


GUARDIAN_MAPPING = JsonListMapping(
    items_path=("response", "results"),
    url_field="webUrl",
    title_field="webTitle",
    time_field="webPublicationDate",
    time_format="iso",
    body_field="fields.bodyText",
)


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, environment="test", **overrides)


# --- news sitemap ---------------------------------------------------------


def test_news_sitemap_maps_loc_title_and_publication_date() -> None:
    result = parse_news_sitemap(fixture("news_sitemap_ok.xml"), source("news_sitemap"))

    assert [(str(c.url), c.headline) for c in result] == [
        (f"https://{HOST}/markets/fed-holds-rates", "Fed holds rates steady"),
        (f"https://{HOST}/markets/tsmc-record", "TSMC posts record quarter"),
    ]
    assert result[0].seen_at == datetime(2026, 9, 2, 2, 30, tzinfo=UTC)
    # +08:00 offsets are normalised to UTC.
    assert result[1].seen_at == datetime(2026, 9, 2, 1, 15, tzinfo=UTC)


def test_news_sitemap_drops_entries_missing_loc_title_or_news_block() -> None:
    result = parse_news_sitemap(fixture("news_sitemap_missing_fields.xml"), source("news_sitemap"))

    assert [c.headline for c in result] == ["Complete entry"]


def test_news_sitemap_keeps_entries_with_unparseable_dates_as_undated() -> None:
    result = parse_news_sitemap(fixture("news_sitemap_bad_time.xml"), source("news_sitemap"))

    assert [(c.headline, c.seen_at) for c in result] == [
        ("Undated entry", None),
        ("Naive timestamp entry", None),
    ]


# --- json list ------------------------------------------------------------


def test_json_list_maps_nested_fields_and_only_keeps_long_bodies() -> None:
    result = parse_json_list(
        fixture("json_list_ok.json"), source("json_list", mapping=GUARDIAN_MAPPING)
    )

    assert [(c.headline, c.seen_at) for c, _ in result] == [
        ("Fed holds rates", datetime(2026, 9, 2, 2, 30, tzinfo=UTC)),
        ("Teaser only", datetime(2026, 9, 2, 1, 0, tzinfo=UTC)),
    ]
    assert result[0][1] is not None and result[0][1].startswith("This paragraph is long")
    assert result[1][1] is None


def test_json_list_skips_items_missing_url_or_title_or_not_objects() -> None:
    result = parse_json_list(
        fixture("json_list_missing_fields.json"), source("json_list", mapping=GUARDIAN_MAPPING)
    )

    assert [c.headline for c, _ in result] == ["Has everything", "No fields block"]
    assert [body for _, body in result] == [None, None]


def test_json_list_treats_unparseable_times_as_undated() -> None:
    result = parse_json_list(
        fixture("json_list_bad_time.json"), source("json_list", mapping=GUARDIAN_MAPPING)
    )

    assert [(c.headline, c.seen_at) for c, _ in result] == [("Bad time", None), ("Null time", None)]


def test_json_list_rejects_payloads_without_the_expected_array() -> None:
    with pytest.raises(ValueError, match="no array"):
        parse_json_list(b'{"response": {}}', source("json_list", mapping=GUARDIAN_MAPPING))
    with pytest.raises(ValueError, match="no mapping"):
        parse_json_list(b"[]", source("json_list"))


def test_json_list_strips_the_jin10_js_prefix_and_builds_urls_from_ids() -> None:
    jin10 = source(
        "json_list",
        hostname="www.jin10.com",
        url="https://www.jin10.com/flash_newest.js",
        mapping=JsonListMapping(
            items_path=(),
            id_field="id",
            url_template="https://www.jin10.com/flash/{id}",
            title_field="data.content",
            time_field="time",
            time_format="datetime_str",
            js_prefix=True,
        ),
    )

    result = parse_json_list(fixture("jin10_flash.js"), jin10)

    assert [str(c.url) for c, _ in result] == [
        "https://www.jin10.com/flash/20260902103000123456",
        "https://www.jin10.com/flash/20260902102000111111",
    ]
    # 10:30 Asia/Shanghai is 02:30 UTC.
    assert result[0][0].seen_at == datetime(2026, 9, 2, 2, 30, tzinfo=UTC)
    assert result[0][0].headline.startswith("央行公开市场")


@pytest.mark.parametrize(
    ("time_format", "value", "expected"),
    [
        ("unix_s", 1788316200, datetime(2026, 9, 2, 2, 30, tzinfo=UTC)),
        ("unix_s", "1788316200", datetime(2026, 9, 2, 2, 30, tzinfo=UTC)),
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

    [(candidate, _)] = parse_json_list(payload, source("json_list", mapping=mapping))

    assert candidate.seen_at == expected


def test_json_list_rejects_unknown_time_formats() -> None:
    mapping = JsonListMapping(
        items_path=(), url_field="url", title_field="title", time_field="t", time_format="epoch"
    )
    payload = b'[{"url": "https://www.example-news.com/a", "title": "A", "t": 1}]'

    with pytest.raises(ValueError, match="unsupported time_format"):
        parse_json_list(payload, source("json_list", mapping=mapping))


# --- full-text RSS and RDF -------------------------------------------------


def test_rss_full_extracts_content_encoded_only_when_long_enough() -> None:
    result = parse_rss_entries(fixture("rss_full_ok.xml"), source("rss_full"))

    assert [c.headline for c, _ in result] == ["Full article", "Teaser article", "No encoded body"]
    body = result[0][1]
    assert body is not None
    assert body.startswith("This paragraph is long")
    assert "alert(1)" not in body
    assert body.endswith("Second paragraph.")
    assert result[1][1] is None
    assert result[2][1] is None


def test_rss_full_skips_items_missing_title_or_link_but_keeps_undated_ones() -> None:
    result = parse_rss(fixture("rss_full_missing_fields.xml"), source("rss_full"))

    assert [(c.headline, c.seen_at) for c in result] == [("Undated but kept", None)]


def test_rss_parses_rdf_items_with_dc_date() -> None:
    result = parse_rss(fixture("rss_rdf.xml"), source("rss"))

    assert [(str(c.url), c.seen_at) for c in result] == [
        (f"https://{HOST}/news/id/20", datetime(2026, 9, 2, 2, 30, tzinfo=UTC)),
        (f"https://{HOST}/news/id/21", None),
    ]


# --- request shaping --------------------------------------------------------


def test_request_url_injects_api_key_and_reports_missing_credentials() -> None:
    guardian = source(
        "json_list",
        url=f"https://{HOST}/search?section=business",
        api_key_setting="guardian_api_key",
    )

    assert request_url(guardian, settings()) is None
    assert request_url(guardian, settings(guardian_api_key=SecretStr("   "))) is None
    url = request_url(guardian, settings(guardian_api_key=SecretStr("abc")))
    assert url == f"https://{HOST}/search?section=business&api-key=abc"


def test_request_url_varies_the_cache_buster_per_request() -> None:
    eastmoney = source("json_list", url=f"https://{HOST}/list?type=1", cache_buster_param="r")

    urls = {request_url(eastmoney, settings()) for _ in range(5)}

    assert len(urls) > 1
    assert all(url is not None and "&r=" in url for url in urls)


def test_request_headers_append_the_contact_email_for_sec() -> None:
    sec = source("rss", contact_email_setting="sec_contact_email")

    assert request_headers(sec, settings()) is None
    assert request_headers(sec, settings(sec_contact_email="nobody")) is None
    assert request_headers(sec, settings(sec_contact_email="ops@example.com")) == {
        "User-Agent": "DailyInsightsNewsBot/1.0 (FPI-TW; ops@example.com)"
    }


async def _run_discovery(
    monkeypatch: pytest.MonkeyPatch,
    sources: list[FeedSource],
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    settings_values: Settings | None = None,
    bodies: dict[str, str] | None = None,
) -> tuple[list[Any], list[tuple[str, dict[str, Any]]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    async def robots(_: httpx.AsyncClient, __: str, ___: frozenset[str]) -> bool:
        return True

    def record(name: str, **fields: Any) -> None:
        events.append((name, fields))

    monkeypatch.setattr(feeds, "robots_allowed", robots)
    monkeypatch.setattr(feeds, "emit_event", record)
    monkeypatch.setattr(feeds, "FEED_SOURCES", sources)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_feed_candidates(
            client, ALLOWED, NOW, settings=settings_values or settings(), bodies=bodies
        )
    return result, events


async def test_discovery_skips_credentialed_feeds_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guardian = source("json_list", mapping=GUARDIAN_MAPPING, api_key_setting="guardian_api_key")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=fixture("json_list_ok.json"))

    result, events = await _run_discovery(monkeypatch, [guardian], handler)

    assert result == [] and requested == []
    assert events == [
        (
            "news.feed.skipped",
            {"hostname": HOST, "feed": guardian.url, "reason": "missing_credential"},
        )
    ]


async def test_discovery_sends_the_key_and_collects_full_text_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guardian = source(
        "json_list",
        mapping=GUARDIAN_MAPPING,
        api_key_setting="guardian_api_key",
        provides_full_text=True,
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=fixture("json_list_ok.json"))

    bodies: dict[str, str] = {}
    result, events = await _run_discovery(
        monkeypatch,
        [guardian],
        handler,
        settings_values=settings(guardian_api_key=SecretStr("abc")),
        bodies=bodies,
    )

    assert requested == [f"https://{HOST}/feed?api-key=abc"]
    assert [c.headline for c in result] == ["Fed holds rates", "Teaser only"]
    assert set(bodies) == {result[0].id}
    ok = [fields for name, fields in events if name == "news.feed.ok"]
    assert ok == [
        {
            "hostname": HOST,
            "feed": guardian.url,
            "market": "global",
            "count": 2,
            "newest_age_minutes": 570,
            "full_text": 1,
            "dropped_language": 0,
        }
    ]


async def test_discovery_skips_sec_without_a_contact_email_and_sends_it_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sec = source("rss", contact_email_setting="sec_contact_email")
    agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        agents.append(request.headers["user-agent"])
        return httpx.Response(200, content=fixture("rss_rdf.xml"))

    _, events = await _run_discovery(monkeypatch, [sec], handler)
    assert agents == []
    assert events[0][0] == "news.feed.skipped"
    assert events[0][1]["reason"] == "missing_contact_email"

    result, _ = await _run_discovery(
        monkeypatch, [sec], handler, settings_values=settings(sec_contact_email="ops@example.com")
    )
    assert agents == ["DailyInsightsNewsBot/1.0 (FPI-TW; ops@example.com)"]
    assert len(result) == 2


async def test_discovery_throttles_consecutive_requests_to_the_same_feed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = source("news_sitemap", url=f"https://{HOST}/sitemap-a.xml", min_interval_seconds=1)
    second = source("news_sitemap", url=f"https://{HOST}/sitemap-b.xml", min_interval_seconds=1)
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    async def robots(_: httpx.AsyncClient, __: str, ___: frozenset[str]) -> bool:
        return True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=fixture("news_sitemap_ok.xml"))

    monkeypatch.setattr(feeds, "robots_allowed", robots)
    monkeypatch.setattr(feeds, "FEED_SOURCES", [first, second])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await discover_feed_candidates(client, ALLOWED, NOW, settings=settings(), sleep=sleep)

    # The first request goes out immediately; the second waits out the interval.
    assert len(waits) == 1 and 0 < waits[0] <= 1


async def test_discovery_reads_eastmoney_style_lists_with_a_fresh_cache_buster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eastmoney = source(
        "json_list",
        hostname="finance.eastmoney.com",
        url="https://finance.eastmoney.com/api/list?type=1",
        cache_buster_param="r",
        mapping=JsonListMapping(
            items_path=("data", "list"),
            url_field="url",
            title_field="title",
            time_field="showTime",
            time_format="datetime_str",
        ),
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=fixture("eastmoney_list.json"))

    result, _ = await _run_discovery(monkeypatch, [eastmoney], handler)
    result_again, _ = await _run_discovery(monkeypatch, [eastmoney], handler)

    assert [c.headline for c in result] == ["沪指高开0.3%", "央行逆回购"]
    assert result == result_again
    assert len(requested) == 2 and requested[0] != requested[1]
    assert all("type=1&r=" in url for url in requested)
