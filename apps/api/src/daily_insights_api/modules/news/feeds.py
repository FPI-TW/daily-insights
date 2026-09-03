"""Candidate discovery: the allowlisted publishers' own feeds and listing pages.

This registry is the only discovery path. Feed URLs are constants owned by
this module (never user input), every fetch is byte-capped and honours
robots.txt, and only article URLs on the allowlisted host survive, so the
downstream SSRF-safe extraction contract is unchanged.
"""

import asyncio
import hashlib
import json
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import httpx
from defusedxml import ElementTree
from pydantic import SecretStr

from daily_insights_api.core.config import Settings, get_settings
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.news.contracts import Candidate
from daily_insights_api.modules.news.editions import GLOBAL_MARKET
from daily_insights_api.modules.news.extraction import (
    MAX_ARTICLE_CHARS,
    _ArticleTextExtractor,
    _dedupe_candidates,
    allowed_hostname,
    robots_allowed,
)

MAX_FEED_BYTES = 2_000_000
MAX_PER_FEED = 10
USER_AGENT = "DailyInsightsNewsBot/1.0"
# A feed-supplied body shorter than this is treated as a teaser, not full text.
MIN_FULL_TEXT_CHARS = 200
FEED_KINDS = frozenset({"rss", "rss_full", "news_sitemap", "json_list", "cnyes_json", "listing"})
POLL_GROUPS = frozenset({"flash", "fast", "normal"})
_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_NEWS_NS = "{http://www.google.com/schemas/sitemap-news/0.9}"
_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
_DC_NS = "{http://purl.org/dc/elements/1.1/}"


@dataclass(frozen=True)
class JsonListMapping:
    """Field mapping for a generic JSON list feed.

    Field names use dot-notation for nested values (``fields.bodyText``);
    ``items_path`` walks to the array. ``time_format`` is one of ``unix_s``,
    ``unix_ms``, ``iso`` or ``datetime_str`` (parsed with
    ``datetime_str_format`` in ``time_zone``).
    """

    items_path: tuple[str, ...]
    title_field: str
    time_field: str
    time_format: str
    id_field: str | None = None
    url_field: str | None = None
    url_template: str | None = None
    body_field: str | None = None
    datetime_str_format: str = "%Y-%m-%d %H:%M:%S"
    time_zone: str = "Asia/Shanghai"
    # Some endpoints return a JS assignment (``var x = {...};``) instead of JSON.
    js_prefix: bool = False


@dataclass(frozen=True)
class FeedSource:
    hostname: str
    url: str
    kind: str
    link_pattern: str | None = None
    host_rewrites: tuple[tuple[str, str], ...] = ()
    # Which editions read this feed; "global" is the daily digest.
    markets: frozenset[str] = frozenset({GLOBAL_MARKET})
    max_items: int = MAX_PER_FEED
    # Publisher name shown to readers; falls back to the hostname.
    display_name: str = ""
    # A feed whose newest item is older than this is reported as stale. Feeds
    # that answer HTTP 200 with months-old items are the most common failure.
    max_age_hours: int = 24
    # The feed carries the article body, so extraction can skip fetch_article.
    provides_full_text: bool = False
    # Suggested polling cadence for a future resident poller: flash | fast | normal.
    poll_group: str = "normal"
    mapping: JsonListMapping | None = None
    # Credentialed feeds: the Settings attribute holding the key and the query
    # parameter to place it in. Keys never appear in FEED_SOURCES.
    api_key_setting: str | None = None
    api_key_param: str = "api-key"
    # Query parameter that must change every request to defeat CDN caching.
    cache_buster_param: str | None = None
    # Settings attribute holding a contact email appended to the User-Agent
    # (SEC EDGAR requires "company contact@email").
    contact_email_setting: str | None = None
    # Minimum spacing between requests to this feed's host (Guardian: 1/s).
    min_interval_seconds: float = 0.0


# Reuters is deliberately absent: it answers non-browser requests with 401, so
# discovered links could never be extracted.
FEED_SOURCES: tuple[FeedSource, ...] = (
    FeedSource(
        "www.cnbc.com",
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",
        "rss",
        r"^https://www\.cnbc\.com/\d{4}/\d{2}/\d{2}/[a-z0-9-]+\.html$",
        markets=frozenset({GLOBAL_MARKET, "us_equity"}),
        display_name="CNBC",
    ),
    FeedSource(
        "www.bbc.com",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "rss",
        r"^https://www\.bbc\.com/news/articles/[a-z0-9]+$",
        host_rewrites=(("www.bbc.co.uk", "www.bbc.com"),),
        display_name="BBC Business",
    ),
    FeedSource(
        "apnews.com",
        "https://apnews.com/business",
        "listing",
        r"^https://apnews\.com/article/[a-z0-9-]+$",
        markets=frozenset({GLOBAL_MARKET, "us_equity"}),
        display_name="AP",
    ),
    # cnyes category pages are rendered client-side (the static HTML only
    # carries the sidebar), so its public JSON list endpoint is used instead.
    FeedSource(
        "news.cnyes.com",
        "https://api.cnyes.com/media/api/v1/newslist/category/headline?limit=30&page=1",
        "cnyes_json",
        r"^https://news\.cnyes\.com/news/id/\d+$",
        display_name="鉅亨",
    ),
    FeedSource(
        "news.cnyes.com",
        "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=30&page=1",
        "cnyes_json",
        r"^https://news\.cnyes\.com/news/id/\d+$",
        markets=frozenset({"tw_equity"}),
        max_items=24,
        display_name="鉅亨",
    ),
    FeedSource(
        "news.cnyes.com",
        "https://api.cnyes.com/media/api/v1/newslist/category/us_stock?limit=30&page=1",
        "cnyes_json",
        r"^https://news\.cnyes\.com/news/id/\d+$",
        markets=frozenset({"us_equity"}),
        max_items=12,
        display_name="鉅亨",
    ),
    FeedSource(
        "finance.eastmoney.com",
        "https://finance.eastmoney.com/a/cywjh.html",
        "listing",
        r"^https://finance\.eastmoney\.com/a/\d+\.html$",
        display_name="東方財富",
    ),
)


class _AnchorCollector(HTMLParser):
    """Collect (href, visible text) pairs, keeping the longest text per href."""

    def __init__(self) -> None:
        super().__init__()
        self.links: dict[str, str] = {}
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = next((value for key, value in attrs if key.lower() == "href" and value), None)
        self._href = href
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._href is None:
            return
        text = " ".join("".join(self._text).split())
        if self._href not in self.links or len(text) > len(self.links[self._href]):
            self.links[self._href] = text
        self._href = None
        self._text = []


def feed_client(timeout_seconds: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=False,
        cookies=None,
        trust_env=False,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, application/json, text/html",
        },
    )


def normalize_article_url(href: str, source: FeedSource) -> str | None:
    absolute = urljoin(source.url, href.strip())
    parsed = urlparse(absolute)
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    hostname = (parsed.hostname or "").lower().rstrip(".")
    for original, replacement in source.host_rewrites:
        if hostname == original:
            hostname = replacement
    if scheme != "https" or hostname != source.hostname or parsed.port not in {None, 443}:
        return None
    normalized = urlunparse(("https", hostname, parsed.path, "", "", ""))
    if source.link_pattern and not re.match(source.link_pattern, normalized):
        return None
    return normalized


def _candidate(url: str, headline: str, seen_at: datetime | None, source: FeedSource) -> Candidate:
    return Candidate(
        id=hashlib.sha256(url.encode()).hexdigest(),
        url=url,
        hostname=source.hostname,
        source_name=source.display_name or source.hostname,
        headline=headline[:1000],
        seen_at=seen_at,
    )


def _within_window(seen_at: datetime | None, start: datetime, end: datetime) -> bool:
    return seen_at is None or start <= seen_at <= end


def filter_window(candidates: list[Candidate], start: datetime, end: datetime) -> list[Candidate]:
    """Keep undated candidates and those published inside the window."""
    return [item for item in candidates if _within_window(item.seen_at, start, end)]


def newest_seen_at(candidates: list[Candidate]) -> datetime | None:
    dated = [item.seen_at for item in candidates if item.seen_at is not None]
    return max(dated) if dated else None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(item: Any, *names: str) -> str:
    """First non-empty text among children whose local (namespace-free) name matches."""
    wanted = set(names)
    for child in item:
        if _local_name(child.tag) in wanted and child.text and child.text.strip():
            return str(child.text)
    return ""


def _parse_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).astimezone(UTC)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _html_to_text(html: str) -> str:
    extractor = _ArticleTextExtractor()
    extractor.feed(html)
    return extractor.text()[:MAX_ARTICLE_CHARS]


def parse_rss(payload: bytes, source: FeedSource) -> list[Candidate]:
    """Parse RSS 2.0 and RSS 1.0/RDF items regardless of namespace prefixes."""
    return [candidate for candidate, _ in parse_rss_entries(payload, source)]


def parse_rss_entries(payload: bytes, source: FeedSource) -> list[tuple[Candidate, str | None]]:
    """RSS/RDF items with an optional full-text body from ``content:encoded``.

    RSS 2.0 nests ``item`` under ``channel``; RSS 1.0 (RDF) places ``item``
    under the root and dates it with ``dc:date``, so items are found by local
    name anywhere in the tree. Bodies shorter than MIN_FULL_TEXT_CHARS are
    teasers (Forbes ships a content:encoded tag holding only a summary) and
    are dropped so extraction fetches the article instead.
    """
    # defusedxml rejects entity expansion and external DTDs; payloads are also
    # byte-capped before reaching the parser.
    root = ElementTree.fromstring(payload)
    result: list[tuple[Candidate, str | None]] = []
    for item in root.iter():
        if _local_name(item.tag) != "item":
            continue
        link = _child_text(item, "link").strip()
        if not link:
            link = str(item.attrib.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", ""))
        title = " ".join(_child_text(item, "title").split())
        seen_at = _parse_timestamp(_child_text(item, "pubDate", "date", "published", "updated"))
        url = normalize_article_url(link, source) if link else None
        if url is None or not title:
            continue
        body: str | None = None
        if source.kind == "rss_full":
            encoded = _child_text(item, "encoded")
            text = _html_to_text(encoded) if encoded else ""
            body = text if len(text) >= MIN_FULL_TEXT_CHARS else None
        result.append((_candidate(url, title, seen_at, source), body))
    return result


def parse_news_sitemap(payload: bytes, source: FeedSource) -> list[Candidate]:
    """Google News sitemap: <url><loc> plus <news:news><news:title|publication_date>."""
    root = ElementTree.fromstring(payload)
    result: list[Candidate] = []
    for entry in root.iter(f"{_SITEMAP_NS}url"):
        loc = (entry.findtext(f"{_SITEMAP_NS}loc") or "").strip()
        news = entry.find(f"{_NEWS_NS}news")
        title = (
            " ".join((news.findtext(f"{_NEWS_NS}title") or "").split()) if news is not None else ""
        )
        published = news.findtext(f"{_NEWS_NS}publication_date") if news is not None else None
        seen_at = _parse_timestamp(published) if published else None
        url = normalize_article_url(loc, source) if loc else None
        if url is None or not title:
            continue
        result.append(_candidate(url, title, seen_at, source))
    return result


def _lookup(item: Any, path: str) -> Any:
    current = item
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _strip_js_prefix(payload: str) -> str:
    """Turn ``var newest = [...];`` into the bare JSON literal."""
    match = re.search(r"[\[{]", payload)
    if match is None:
        raise ValueError("no JSON literal in JS payload")
    return payload[match.start() :].rstrip().rstrip(";")


def _json_time(value: Any, mapping: JsonListMapping) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if mapping.time_format in {"unix_s", "unix_ms"}:
            number = float(value)
            if mapping.time_format == "unix_ms":
                number /= 1000
            return datetime.fromtimestamp(number, UTC)
        if mapping.time_format == "iso":
            return _parse_timestamp(str(value))
        if mapping.time_format == "datetime_str":
            naive = datetime.strptime(str(value).strip(), mapping.datetime_str_format)
            return naive.replace(tzinfo=ZoneInfo(mapping.time_zone)).astimezone(UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    raise ValueError(f"unsupported time_format {mapping.time_format}")


def parse_json_list(payload: bytes, source: FeedSource) -> list[tuple[Candidate, str | None]]:
    """Generic JSON list adapter driven by ``FeedSource.mapping``."""
    mapping = source.mapping
    if mapping is None:
        raise ValueError("json_list source has no mapping")
    text = payload.decode("utf-8", errors="replace")
    if mapping.js_prefix:
        text = _strip_js_prefix(text)
    document = json.loads(text)
    items: Any = document
    for part in mapping.items_path:
        items = items.get(part) if isinstance(items, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"json list has no array at {'.'.join(mapping.items_path) or '$'}")
    result: list[tuple[Candidate, str | None]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title_value = _lookup(item, mapping.title_field)
        title = " ".join(str(title_value).split()) if isinstance(title_value, str) else ""
        if mapping.url_field:
            raw_url = _lookup(item, mapping.url_field)
        elif mapping.url_template and mapping.id_field:
            identifier = _lookup(item, mapping.id_field)
            raw_url = (
                mapping.url_template.format(id=identifier)
                if isinstance(identifier, int | str) and str(identifier)
                else None
            )
        else:
            raw_url = None
        url = normalize_article_url(str(raw_url), source) if isinstance(raw_url, str) else None
        if url is None or not title:
            continue
        seen_at = _json_time(_lookup(item, mapping.time_field), mapping)
        body: str | None = None
        if mapping.body_field:
            body_value = _lookup(item, mapping.body_field)
            if isinstance(body_value, str):
                text_body = (
                    _html_to_text(body_value) if "<" in body_value else " ".join(body_value.split())
                )
                body = (
                    text_body[:MAX_ARTICLE_CHARS] if len(text_body) >= MIN_FULL_TEXT_CHARS else None
                )
        result.append((_candidate(url, title, seen_at, source), body))
    return result


def parse_cnyes_json(payload: bytes, source: FeedSource) -> list[Candidate]:
    """Map the cnyes list endpoint (items.data[] with newsId/title/publishAt)."""
    document = json.loads(payload)
    items = document.get("items") if isinstance(document, dict) else None
    data = items.get("data") if isinstance(items, dict) else None
    if not isinstance(data, list):
        raise ValueError("cnyes list has no items.data array")
    result: list[Candidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        news_id, title, published = item.get("newsId"), item.get("title"), item.get("publishAt")
        if not isinstance(news_id, int) or not isinstance(title, str) or not title.strip():
            continue
        seen_at = (
            datetime.fromtimestamp(published, UTC) if isinstance(published, int | float) else None
        )
        url = normalize_article_url(f"https://{source.hostname}/news/id/{news_id}", source)
        if url is None:
            continue
        result.append(_candidate(url, " ".join(title.split()), seen_at, source))
    return result


def parse_listing(payload: str, source: FeedSource) -> list[Candidate]:
    collector = _AnchorCollector()
    collector.feed(payload)
    result: list[Candidate] = []
    seen: set[str] = set()
    for href, text in collector.links.items():
        url = normalize_article_url(href, source)
        if url is None or url in seen:
            continue
        headline = text if len(text) >= 8 else _slug_headline(url)
        if not headline:
            continue
        seen.add(url)
        result.append(_candidate(url, headline, None, source))
    return result


def _slug_headline(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    words = [word for word in re.split(r"[-_]+", slug) if word and not word.isdigit()]
    return " ".join(words) if len(words) >= 3 else ""


async def _read_capped(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> bytes:
    async with client.stream("GET", url, headers=headers) as response:
        if response.is_redirect:
            raise ValueError("feed redirected")
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_FEED_BYTES:
                raise ValueError("feed exceeds size limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _with_query(url: str, extra: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(extra)
    return urlunparse(parsed._replace(query=urlencode(query)))


def request_url(source: FeedSource, settings: Settings) -> str | None:
    """Final URL for one request, or None when a required credential is missing."""
    extra: dict[str, str] = {}
    if source.api_key_setting:
        secret = getattr(settings, source.api_key_setting, None)
        key = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        if not isinstance(key, str) or not key.strip():
            return None
        extra[source.api_key_param] = key.strip()
    if source.cache_buster_param:
        extra[source.cache_buster_param] = str(random.randint(10**9, 10**10 - 1))
    return _with_query(source.url, extra) if extra else source.url


def request_headers(source: FeedSource, settings: Settings) -> dict[str, str] | None:
    if not source.contact_email_setting:
        return None
    email = getattr(settings, source.contact_email_setting, None)
    if not isinstance(email, str) or "@" not in email:
        return None
    return {"User-Agent": f"{USER_AGENT} (FPI-TW; {email})"}


def parse_feed(source: FeedSource, payload: bytes) -> list[tuple[Candidate, str | None]]:
    if source.kind in {"rss", "rss_full"}:
        return parse_rss_entries(payload, source)
    if source.kind == "news_sitemap":
        return [(candidate, None) for candidate in parse_news_sitemap(payload, source)]
    if source.kind == "json_list":
        return parse_json_list(payload, source)
    if source.kind == "cnyes_json":
        return [(candidate, None) for candidate in parse_cnyes_json(payload, source)]
    if source.kind == "listing":
        return [
            (candidate, None)
            for candidate in parse_listing(payload.decode("utf-8", errors="replace"), source)
        ]
    raise ValueError(f"unknown feed kind {source.kind}")


async def discover_feed_candidates(
    client: httpx.AsyncClient,
    allowed: frozenset[str],
    now: datetime | None = None,
    *,
    market: str = GLOBAL_MARKET,
    settings: Settings | None = None,
    bodies: dict[str, str] | None = None,
    sleep: Any = asyncio.sleep,
) -> list[Candidate]:
    """Read every feed tagged for ``market`` whose article host is allowlisted.

    Failures are isolated per feed and reported as events; the function never
    raises, so a broken publisher cannot take the whole edition down. When a
    ``bodies`` dict is supplied, full-text feeds store each candidate's body
    there (keyed by candidate id) so extraction can skip fetching it. Bodies
    live in memory only; they are never persisted.
    """
    end = now or datetime.now(UTC)
    start = end - timedelta(hours=24)
    resolved_settings = settings or get_settings()
    result: list[Candidate] = []
    last_request: dict[str, float] = {}
    for source in FEED_SOURCES:
        if market not in source.markets or not allowed_hostname(source.hostname, allowed):
            continue
        url = request_url(source, resolved_settings)
        if url is None:
            emit_event(
                "news.feed.skipped",
                hostname=source.hostname,
                feed=source.url,
                reason="missing_credential",
            )
            continue
        feed_host = (urlparse(source.url).hostname or "").lower()
        headers = request_headers(source, resolved_settings)
        if source.contact_email_setting and headers is None:
            emit_event(
                "news.feed.skipped",
                hostname=source.hostname,
                feed=source.url,
                reason="missing_contact_email",
            )
            continue
        try:
            if source.min_interval_seconds:
                elapsed = asyncio.get_running_loop().time() - last_request.get(feed_host, -1e9)
                if elapsed < source.min_interval_seconds:
                    await sleep(source.min_interval_seconds - elapsed)
                last_request[feed_host] = asyncio.get_running_loop().time()
            if not await robots_allowed(client, url, allowed | {feed_host}):
                raise ValueError("robots disallow feed")
            payload = await _read_capped(client, url, headers)
            entries = parse_feed(source, payload)
        except Exception as error:
            emit_event(
                "news.feed.failed",
                hostname=source.hostname,
                feed=source.url,
                error_code=type(error).__name__,
            )
            continue
        parsed = [candidate for candidate, _ in entries]
        # Freshness is judged before the window filter: a feed whose newest
        # item is days old would otherwise look like an empty feed. Stale
        # feeds still contribute so selection, not discovery, decides.
        newest = newest_seen_at(parsed)
        newest_age_minutes = (
            int((end - newest).total_seconds() // 60) if newest is not None else None
        )
        if newest is not None and end - newest > timedelta(hours=source.max_age_hours):
            emit_event(
                "news.feed.stale",
                hostname=source.hostname,
                feed=source.url,
                market=market,
                age_hours=round((end - newest).total_seconds() / 3600, 1),
                max_age_hours=source.max_age_hours,
            )
        found = filter_window(parsed, start, end)[: source.max_items]
        if bodies is not None and source.provides_full_text:
            kept = {candidate.id for candidate in found}
            for candidate, body in entries:
                if body and candidate.id in kept:
                    bodies[candidate.id] = body
        emit_event(
            "news.feed.ok",
            hostname=source.hostname,
            feed=source.url,
            market=market,
            count=len(found),
            newest_age_minutes=newest_age_minutes,
            full_text=sum(1 for candidate in found if bodies and candidate.id in bodies),
        )
        result.extend(found)
    return _dedupe_candidates(result)
