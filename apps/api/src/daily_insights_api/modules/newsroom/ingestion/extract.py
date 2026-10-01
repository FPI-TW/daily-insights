"""HTML → plain text for article pages and feed-embedded bodies."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

from daily_insights_api.modules.newsroom.models import BODY_MAX_CHARS


class _ArticleTextExtractor(HTMLParser):
    """Prefers ``<article>``, then ``<main>``, then the whole page; drops chrome."""

    _SKIP = frozenset(
        {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form"}
    )

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = self._article_depth = self._main_depth = 0
        self._in_title = self._in_json_ld = False
        self.article_parts: list[str] = []
        self.main_parts: list[str] = []
        self.fallback_parts: list[str] = []
        self.title_parts: list[str] = []
        self.json_ld_parts: list[str] = []
        self.og_title: str | None = None
        self.published_at: datetime | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag == "meta":
            prop = values.get("property", "").lower()
            if prop == "article:published_time":
                try:
                    published_at = datetime.fromisoformat(values["content"].replace("Z", "+00:00"))
                    if published_at.tzinfo is not None and published_at.utcoffset() is not None:
                        self.published_at = published_at.astimezone(UTC)
                except (KeyError, ValueError):
                    pass
            elif prop == "og:title" and values.get("content", "").strip():
                self.og_title = " ".join(values["content"].split())
        if tag == "title":
            self._in_title = True
        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._in_json_ld = True
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag == "article":
            self._article_depth += 1
        if tag == "main":
            self._main_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script":
            self._in_json_ld = False
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "article" and self._article_depth:
            self._article_depth -= 1
        if tag == "main" and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_json_ld:
            self.json_ld_parts.append(data)
        if self._skip_depth:
            return
        if self._article_depth:
            self.article_parts.append(data)
        if self._main_depth:
            self.main_parts.append(data)
        self.fallback_parts.append(data)

    def text(self) -> str:
        return re.sub(
            r"\s+", " ", " ".join(self.article_parts or self.main_parts or self.fallback_parts)
        ).strip()


def _json_ld_marks_paywall(blobs: list[str]) -> bool:
    """schema.org ``isAccessibleForFree: false`` is how publishers declare a paywall."""
    for blob in blobs:
        try:
            document = json.loads(blob)
        except ValueError:
            continue
        stack = [document]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                flag = node.get("isAccessibleForFree")
                if flag is False or (isinstance(flag, str) and flag.strip().lower() == "false"):
                    return True
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return False


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    text: str
    title: str | None
    published_at: datetime | None
    paywalled: bool


def extract_page(html: str) -> ExtractedPage:
    parser = _ArticleTextExtractor()
    parser.feed(html)
    parser.close()
    title = parser.og_title or " ".join("".join(parser.title_parts).split()) or None
    return ExtractedPage(
        text=parser.text()[:BODY_MAX_CHARS],
        title=title[:1000] if title else None,
        published_at=parser.published_at,
        paywalled=_json_ld_marks_paywall(parser.json_ld_parts),
    )


def html_to_text(html: str) -> str:
    parser = _ArticleTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()[:BODY_MAX_CHARS]
