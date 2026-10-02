"""Feed adapters: RSS, RDF, Atom, full-text RSS, Google News sitemaps, JSON lists.

Moved from ``news/feeds.py``. Parsers are pure: they take the bytes of one feed
and a ``SourceConfig`` built from a ``newsroom_sources`` row and return entries
whose URLs are already normalised to the source's article host. Anything on a
different host, or not matching the source's ``link_pattern``, is dropped here,
so downstream fetching only ever sees allowlisted publisher URLs.
"""

import hashlib
import json
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

from defusedxml import ElementTree
from langdetect import DetectorFactory, LangDetectException, detect

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.ingestion.extract import html_to_text
from daily_insights_api.modules.newsroom.ingestion.safe_http import USER_AGENT, normalize_hostname
from daily_insights_api.modules.newsroom.models import BODY_MAX_CHARS, NewsroomSource

MAX_FEED_BYTES = 2_000_000
# A feed-supplied body shorter than this is a teaser, not full text.
MIN_FULL_TEXT_CHARS = 200
MAX_SUMMARY_CHARS = 1_000
MAX_TITLE_CHARS = 1_000

# langdetect is non-deterministic unless seeded.
DetectorFactory.seed = 0
_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_NEWS_NS = "{http://www.google.com/schemas/sitemap-news/0.9}"
_RDF_ABOUT = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"
XML_KINDS = frozenset({"rss", "rdf", "atom", "rss_full"})


class MissingCredentialError(Exception):
    """A source needs a setting that is not configured; polling skips it."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
    summary_field: str | None = None
    datetime_str_format: str = "%Y-%m-%d %H:%M:%S"
    time_zone: str = "Asia/Shanghai"
    # Some endpoints return a JS assignment (``var x = {...};``) instead of JSON.
    js_prefix: bool = False
    # Flash feeds often leave the title empty and carry the text in the body.
    title_fallback_field: str | None = None
    min_body_chars: int = MIN_FULL_TEXT_CHARS

    @classmethod
    def from_options(cls, value: Any) -> "JsonListMapping":
        if not isinstance(value, Mapping):
            raise ValueError("json_list source has no mapping")
        known = {item.name for item in fields(cls)}
        unknown = set(value) - known
        if unknown:
            raise ValueError(f"unknown json_list mapping keys: {sorted(unknown)}")
        data = dict(value)
        data["items_path"] = tuple(data.get("items_path", ()))
        return cls(**data)


GUARDIAN_MAPPING = JsonListMapping(
    items_path=("response", "results"),
    url_field="webUrl",
    title_field="webTitle",
    time_field="webPublicationDate",
    time_format="iso",
    body_field="fields.bodyText",
    summary_field="fields.trailText",
)
GUARDIAN_API_HOST = "content.guardianapis.com"


@dataclass(frozen=True)
class SourceConfig:
    """What the adapters need from a ``newsroom_sources`` row.

    ``options`` carries adapter parameters (seed migration 20261001_0032):
    ``host_rewrites`` ([[from, to], ...]), ``keep_query``, ``naive_time_zone``,
    ``min_interval_seconds``, ``cache_buster_param``, ``requires_contact_email``
    and, for ``json_list``, ``mapping``. Credentials are never named by
    options: ``guardian_api`` always uses ``guardian_api_key`` and only sends it
    to the Guardian API host.
    """

    key: str
    kind: str
    url: str
    hostname: str
    link_pattern: str | None = None
    language_filter: frozenset[str] | None = None
    full_text_in_feed: bool = False
    options: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: NewsroomSource) -> "SourceConfig":
        return cls(
            key=row.key,
            kind=row.kind,
            url=row.url or "",
            hostname=normalize_hostname(row.hostname),
            link_pattern=row.link_pattern,
            language_filter=frozenset(row.language_filter) if row.language_filter else None,
            full_text_in_feed=row.full_text_in_feed,
            options=dict(row.options or {}),
        )

    @property
    def feed_host(self) -> str:
        return normalize_hostname(urlparse(self.url).hostname or "")

    @property
    def host_rewrites(self) -> dict[str, str]:
        pairs = self.options.get("host_rewrites") or []
        return {
            normalize_hostname(str(pair[0])): normalize_hostname(str(pair[1]))
            for pair in pairs
            if isinstance(pair, list | tuple) and len(pair) == 2
        }

    @property
    def keep_query(self) -> bool:
        return self.options.get("keep_query") is True

    @property
    def naive_time_zone(self) -> str | None:
        value = self.options.get("naive_time_zone")
        return value if isinstance(value, str) and value else None

    @property
    def min_interval_seconds(self) -> float:
        value = self.options.get("min_interval_seconds")
        if isinstance(value, bool) or not isinstance(value, int | float):
            return 0.0
        return max(float(value), 0.0)

    @property
    def wants_body(self) -> bool:
        return self.full_text_in_feed or self.kind == "rss_full"


@dataclass(frozen=True, slots=True)
class FeedEntry:
    url: str
    title: str
    published_at: datetime | None
    summary: str | None = None
    body: str | None = None


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def normalize_article_url(href: str, source: SourceConfig) -> str | None:
    """HTTPS URL on the source's article host, or None when the link is not a story."""
    absolute = urljoin(source.url, href.strip())
    parsed = urlparse(absolute)
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    hostname = normalize_hostname(parsed.hostname or "")
    hostname = source.host_rewrites.get(hostname, hostname)
    if scheme != "https" or hostname != source.hostname or parsed.port not in {None, 443}:
        return None
    query = parsed.query if source.keep_query else ""
    normalized = urlunparse(("https", hostname, parsed.path, "", query, ""))
    if source.link_pattern and not re.match(source.link_pattern, normalized):
        return None
    return normalized


def normalize_manual_url(url: str) -> str:
    """Canonical form of an admin-submitted URL: lower-case host, no fragment or port."""
    parsed = urlparse(url.strip())
    hostname = normalize_hostname(parsed.hostname or "")
    return urlunparse(("https", hostname, parsed.path or "/", "", parsed.query, ""))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(item: Any, *names: str) -> str:
    """First non-empty text among children whose local (namespace-free) name matches."""
    wanted = set(names)
    for child in item:
        if _local_name(child.tag) in wanted and child.text and child.text.strip():
            return str(child.text)
    return ""


def parse_timestamp(value: str, naive_zone: str | None = None) -> datetime | None:
    """RFC 2822 or ISO 8601 to UTC; offset-less values need ``naive_zone`` or are dropped."""
    text = value.strip()
    if not text:
        return None
    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        if naive_zone is None:
            return None
        parsed = parsed.replace(tzinfo=ZoneInfo(naive_zone))
    return parsed.astimezone(UTC)


def _summary(raw: str) -> str | None:
    text = html_to_text(raw) if "<" in raw else " ".join(raw.split())
    return text[:MAX_SUMMARY_CHARS] or None


def parse_xml_entries(payload: bytes, source: SourceConfig) -> list[FeedEntry]:
    """RSS 2.0, RSS 1.0 (RDF) and Atom entries, regardless of namespace prefixes.

    RSS 2.0 nests ``item`` under ``channel``; RDF places ``item`` under the
    root and dates it with ``dc:date``; Atom uses ``entry`` with ``link href``
    and ``updated``. Full-text sources take the body from ``content:encoded``
    (or a long ``description``); shorter bodies are teasers and are dropped so
    the article is fetched instead.
    """
    # defusedxml rejects entity expansion and external DTDs; payloads are also
    # byte-capped before reaching the parser.
    root = ElementTree.fromstring(payload)
    result: list[FeedEntry] = []
    for item in root.iter():
        if _local_name(item.tag) not in {"item", "entry"}:
            continue
        link = _child_text(item, "link").strip()
        if not link:
            link = next(
                (
                    str(child.attrib["href"])
                    for child in item
                    if _local_name(child.tag) == "link" and child.attrib.get("href")
                ),
                str(item.attrib.get(_RDF_ABOUT, "")),
            )
        title = " ".join(_child_text(item, "title").split())[:MAX_TITLE_CHARS]
        published_at = parse_timestamp(
            _child_text(item, "pubDate", "date", "published", "updated"), source.naive_time_zone
        )
        url = normalize_article_url(link, source) if link else None
        if url is None or not title:
            continue
        description = _child_text(item, "description", "summary")
        body: str | None = None
        if source.wants_body:
            raw = _child_text(item, "encoded", "content") or description
            text = html_to_text(raw) if raw else ""
            body = text if len(text) >= MIN_FULL_TEXT_CHARS else None
        summary = _summary(description) if description else None
        result.append(FeedEntry(url, title, published_at, summary, body))
    return result


def parse_news_sitemap(payload: bytes, source: SourceConfig) -> list[FeedEntry]:
    """Google News sitemap: <url><loc> plus <news:news><news:title|publication_date>."""
    root = ElementTree.fromstring(payload)
    result: list[FeedEntry] = []
    for entry in root.iter(f"{_SITEMAP_NS}url"):
        loc = (entry.findtext(f"{_SITEMAP_NS}loc") or "").strip()
        news = entry.find(f"{_NEWS_NS}news")
        title = (
            " ".join((news.findtext(f"{_NEWS_NS}title") or "").split()) if news is not None else ""
        )
        published = news.findtext(f"{_NEWS_NS}publication_date") if news is not None else None
        published_at = parse_timestamp(published, source.naive_time_zone) if published else None
        url = normalize_article_url(loc, source) if loc else None
        if url is None or not title:
            continue
        result.append(FeedEntry(url, title[:MAX_TITLE_CHARS], published_at))
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
            return parse_timestamp(str(value))
        if mapping.time_format == "datetime_str":
            naive = datetime.strptime(str(value).strip(), mapping.datetime_str_format)
            return naive.replace(tzinfo=ZoneInfo(mapping.time_zone)).astimezone(UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    raise ValueError(f"unsupported time_format {mapping.time_format}")


def parse_json_list(
    payload: bytes, source: SourceConfig, mapping: JsonListMapping
) -> list[FeedEntry]:
    """Generic JSON list adapter driven by a field mapping."""
    text = payload.decode("utf-8", errors="replace")
    if mapping.js_prefix:
        text = _strip_js_prefix(text)
    document = json.loads(text)
    items: Any = document
    for part in mapping.items_path:
        items = items.get(part) if isinstance(items, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"json list has no array at {'.'.join(mapping.items_path) or '$'}")
    result: list[FeedEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title_value = _lookup(item, mapping.title_field)
        title = " ".join(str(title_value).split()) if isinstance(title_value, str) else ""
        if not title and mapping.title_fallback_field:
            fallback = _lookup(item, mapping.title_fallback_field)
            if isinstance(fallback, str):
                title = " ".join(html_to_text(fallback).split())[:200]
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
        published_at = _json_time(_lookup(item, mapping.time_field), mapping)
        body: str | None = None
        if mapping.body_field and source.wants_body:
            body_value = _lookup(item, mapping.body_field)
            if isinstance(body_value, str):
                text_body = (
                    html_to_text(body_value) if "<" in body_value else " ".join(body_value.split())
                )
                body = (
                    text_body[:BODY_MAX_CHARS] if len(text_body) >= mapping.min_body_chars else None
                )
        summary: str | None = None
        if mapping.summary_field:
            summary_value = _lookup(item, mapping.summary_field)
            if isinstance(summary_value, str) and summary_value.strip():
                summary = _summary(summary_value)
        result.append(FeedEntry(url, title[:MAX_TITLE_CHARS], published_at, summary, body))
    return result


def parse_feed(source: SourceConfig, payload: bytes) -> list[FeedEntry]:
    if source.kind in XML_KINDS:
        return parse_xml_entries(payload, source)
    if source.kind == "news_sitemap":
        return parse_news_sitemap(payload, source)
    if source.kind == "json_list":
        return parse_json_list(
            payload, source, JsonListMapping.from_options(source.options.get("mapping"))
        )
    if source.kind == "guardian_api":
        return parse_json_list(payload, source, GUARDIAN_MAPPING)
    raise ValueError(f"unknown feed kind {source.kind}")


def _with_query(url: str, extra: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(extra)
    return urlunparse(parsed._replace(query=urlencode(query)))


def build_request(source: SourceConfig, settings: Settings) -> tuple[str, dict[str, str]]:
    """Final URL and extra headers for one poll.

    Raises ``MissingCredentialError`` when the source needs a setting that is
    unset (Guardian API key, SEC contact email); polling skips such sources.
    """
    extra: dict[str, str] = {}
    headers: dict[str, str] = {}
    if source.kind == "guardian_api":
        if source.feed_host != GUARDIAN_API_HOST:
            raise ValueError("guardian_api sources must use the Guardian Content API host")
        secret = settings.guardian_api_key
        key = secret.get_secret_value().strip() if secret is not None else ""
        if not key:
            raise MissingCredentialError("guardian_api_key_missing")
        extra["api-key"] = key
    cache_buster = source.options.get("cache_buster_param")
    if isinstance(cache_buster, str) and cache_buster:
        extra[cache_buster] = str(random.randint(10**9, 10**10 - 1))
    if source.options.get("requires_contact_email") is True:
        email = (settings.sec_contact_email or "").strip()
        if "@" not in email:
            raise MissingCredentialError("sec_contact_email_missing")
        headers["User-Agent"] = f"{USER_AGENT} (FPI-TW; {email})"
    return (_with_query(source.url, extra) if extra else source.url), headers


def detected_language(text: str) -> str | None:
    try:
        return str(detect(text))[:10]
    except LangDetectException:
        return None


def entry_language(entry: FeedEntry) -> str | None:
    sample = entry.title
    if entry.summary:
        sample = f"{sample} {entry.summary[:300]}"
    elif entry.body:
        sample = f"{sample} {entry.body[:500]}"
    return detected_language(sample)


def keeps_language(language: str | None, kept: frozenset[str] | None) -> bool:
    """Undetectable text (numbers, tickers) is kept; only a confident foreign language drops."""
    return kept is None or language is None or language in kept
