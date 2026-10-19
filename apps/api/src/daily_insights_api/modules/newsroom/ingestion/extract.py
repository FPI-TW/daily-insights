"""HTML → plain text for article pages and feed-embedded bodies."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

from daily_insights_api.modules.newsroom.ingestion.quality import strip_menu_runs
from daily_insights_api.modules.newsroom.models import BODY_MAX_CHARS

# <article> text shorter than this share of <main> text is a card, not the story.
# Rendered pages, 2026-10-19: stories are 0.19-1.0 of <main>, a card was 0.03.
MIN_ARTICLE_SHARE = 0.1
# A marked article-body container shorter than this is a teaser or a stub.
MIN_MARKED_BODY_CHARS = 200
# Class tokens publishers use for the story container (CNBC ArticleBody-articleBody).
_BODY_CLASSES = frozenset(
    {"article-body", "articlebody", "article__body", "article-content", "story-body"}
)


def _marks_body(values: dict[str, str]) -> bool:
    if values.get("itemprop", "").strip().lower() == "articlebody":
        return True
    return any(
        token in _BODY_CLASSES or token.endswith("-articlebody")
        for token in values.get("class", "").lower().split()
    )


class _Container:
    """Tracks one marked element by counting nested tags of the same name."""

    def __init__(self) -> None:
        self.tag: str | None = None
        self.depth = 0

    def start(self, tag: str, marked: bool) -> None:
        if self.depth and tag == self.tag:
            self.depth += 1
        elif not self.depth and marked:
            self.tag, self.depth = tag, 1

    def end(self, tag: str) -> None:
        if self.depth and tag == self.tag:
            self.depth -= 1


class _ArticleTextExtractor(HTMLParser):
    """Prefers a marked article body, then ``<article>``, then ``<main>``, then the page.

    A marked body is ``itemprop="articleBody"`` or a common article-body class;
    ``role="main"`` counts as ``<main>``. Chrome is dropped.

    ``<form>`` itself is kept: ASP.NET sites (MoneyDJ) wrap the whole page,
    article included, in one. Only its controls are dropped. The ``<title>``
    is never part of the body, so a page of menus cannot pass the title check
    by echoing the headline.
    """

    _SKIP = frozenset(
        {
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "header",
            "footer",
            "aside",
            "button",
            "select",
            "textarea",
            "label",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = self._article_depth = self._main_depth = 0
        self._body = _Container()
        self._role_main = _Container()
        self._in_title = self._in_json_ld = False
        self.body_parts: list[str] = []
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
        self._body.start(tag, _marks_body(values))
        self._role_main.start(tag, values.get("role", "").strip().lower() == "main")

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
        self._body.end(tag)
        self._role_main.end(tag)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._in_json_ld:
            self.json_ld_parts.append(data)
        if self._skip_depth:
            return
        if self._body.depth:
            self.body_parts.append(data)
        if self._article_depth:
            self.article_parts.append(data)
        if self._main_depth or self._role_main.depth:
            self.main_parts.append(data)
        self.fallback_parts.append(data)

    def text(self) -> str:
        body = " ".join(self.body_parts)
        article, main = " ".join(self.article_parts), " ".join(self.main_parts)
        # A rendered page can carry a small <article> card (a "related" list)
        # while the story sits in <main> without one; the card is not the story.
        if len(" ".join(body.split())) >= MIN_MARKED_BODY_CHARS:
            chosen = body
        elif article.strip() and len(article) >= MIN_ARTICLE_SHARE * len(main):
            chosen = article
        else:
            chosen = main if main.strip() else " ".join(self.fallback_parts)
        # Sites without <nav> markup leave their menus in the text as runs of
        # short words; those runs are dropped before the body is judged.
        return strip_menu_runs(re.sub(r"\s+", " ", chosen).strip())


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
