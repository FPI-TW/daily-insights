"""Second discovery path: the allowlisted sources' own RSS feeds and listing pages.

GDELT's HTTPS endpoint is slow and regularly unreachable, so discovery also
reads each allowlisted publisher directly. Feed URLs are constants owned by
this module (never user input), every fetch is byte-capped and honours
robots.txt, and only article URLs on the allowlisted host survive, so the
downstream SSRF-safe extraction contract is unchanged.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from defusedxml import ElementTree

from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.news.contracts import Candidate
from daily_insights_api.modules.news.sources import (
    SOURCE_NAMES,
    _dedupe_candidates,
    allowed_hostname,
    robots_allowed,
)

MAX_FEED_BYTES = 2_000_000
MAX_PER_FEED = 10
USER_AGENT = "DailyInsightsNewsBot/1.0"


@dataclass(frozen=True)
class FeedSource:
    hostname: str
    url: str
    kind: str
    link_pattern: str | None = None
    host_rewrites: tuple[tuple[str, str], ...] = ()


# Reuters is deliberately absent: it answers non-browser requests with 401, so
# discovered links could never be extracted.
FEED_SOURCES: tuple[FeedSource, ...] = (
    FeedSource(
        "www.cnbc.com",
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",
        "rss",
        r"^https://www\.cnbc\.com/\d{4}/\d{2}/\d{2}/[a-z0-9-]+\.html$",
    ),
    FeedSource(
        "www.bbc.com",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "rss",
        r"^https://www\.bbc\.com/news/articles/[a-z0-9]+$",
        host_rewrites=(("www.bbc.co.uk", "www.bbc.com"),),
    ),
    FeedSource(
        "apnews.com",
        "https://apnews.com/business",
        "listing",
        r"^https://apnews\.com/article/[a-z0-9-]+$",
    ),
    FeedSource(
        "news.cnyes.com",
        "https://news.cnyes.com/news/cat/headline",
        "listing",
        r"^https://news\.cnyes\.com/news/id/\d+$",
    ),
    FeedSource(
        "finance.eastmoney.com",
        "https://finance.eastmoney.com/a/cywjh.html",
        "listing",
        r"^https://finance\.eastmoney\.com/a/\d+\.html$",
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
            "Accept": "application/rss+xml, application/xml, text/xml, text/html",
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
        source_name=SOURCE_NAMES.get(source.hostname, source.hostname),
        headline=headline[:1000],
        seen_at=seen_at,
    )


def _within_window(seen_at: datetime | None, start: datetime, end: datetime) -> bool:
    return seen_at is None or start <= seen_at <= end


def parse_rss(
    payload: bytes, source: FeedSource, start: datetime, end: datetime
) -> list[Candidate]:
    # defusedxml rejects entity expansion and external DTDs; payloads are also
    # byte-capped before reaching the parser.
    root = ElementTree.fromstring(payload)
    result: list[Candidate] = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        title = " ".join((item.findtext("title") or "").split())
        published = item.findtext("pubDate")
        seen_at: datetime | None = None
        if published:
            try:
                seen_at = parsedate_to_datetime(published).astimezone(UTC)
            except (TypeError, ValueError):
                seen_at = None
        url = normalize_article_url(link, source) if link else None
        if url is None or not title or not _within_window(seen_at, start, end):
            continue
        result.append(_candidate(url, title, seen_at, source))
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


async def _read_capped(client: httpx.AsyncClient, url: str) -> bytes:
    async with client.stream("GET", url) as response:
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


async def discover_feed_candidates(
    client: httpx.AsyncClient,
    allowed: frozenset[str],
    now: datetime | None = None,
) -> list[Candidate]:
    """Read every configured feed whose article host is allowlisted.

    Failures are isolated per feed and reported as events; the function never
    raises, so a broken publisher cannot take the whole edition down.
    """
    end = now or datetime.now(UTC)
    start = end - timedelta(hours=24)
    result: list[Candidate] = []
    for source in FEED_SOURCES:
        if not allowed_hostname(source.hostname, allowed):
            continue
        feed_host = (urlparse(source.url).hostname or "").lower()
        try:
            if not await robots_allowed(client, source.url, allowed | {feed_host}):
                raise ValueError("robots disallow feed")
            payload = await _read_capped(client, source.url)
            if source.kind == "rss":
                found = parse_rss(payload, source, start, end)
            else:
                found = parse_listing(payload.decode("utf-8", errors="replace"), source)
        except Exception as error:
            emit_event(
                "news.feed.failed",
                hostname=source.hostname,
                error_code=type(error).__name__,
            )
            continue
        found = found[:MAX_PER_FEED]
        emit_event("news.feed.discovered", hostname=source.hostname, count=len(found))
        result.extend(found)
    return _dedupe_candidates(result)
