"""Body quality gate: is this extracted text the article, or page chrome? (spec §6.1)

Three checks, in order; the first failure is the ``body_quality_reason``:

1. ``too_short`` — fewer than ``MIN_BODY_CHARS`` characters after whitespace
   collapsing (the legacy extractor's paywall/teaser floor).
2. ``navigation_heavy`` — more than ``MAX_NAVIGATION_RATIO`` of the text is
   navigation / account / sharing boilerplate (menus, cookie banners, consent
   walls that slipped past the ``<nav>``/``<footer>`` stripping).
3. ``unrelated_to_title`` — fewer than ``MIN_TITLE_OVERLAP`` of the title's
   tokens appear in the body (an index page, a redirect to the home page, or
   another story). Latin titles use words of 3+ letters minus stopwords; CJK
   titles use character bigrams. Titles with fewer than ``MIN_TITLE_TOKENS``
   tokens skip this check because the ratio would be noise.
"""

import re
from dataclasses import dataclass

MIN_BODY_CHARS = 200
MAX_NAVIGATION_RATIO = 0.10
MIN_TITLE_OVERLAP = 0.2
MIN_TITLE_TOKENS = 3

_NAVIGATION_TERMS = (
    # English
    "subscribe",
    "subscription",
    "sign in",
    "sign up",
    "log in",
    "login",
    "newsletter",
    "cookie",
    "cookies",
    "privacy policy",
    "terms of use",
    "terms of service",
    "all rights reserved",
    "advertisement",
    "related articles",
    "read more",
    "share this",
    "follow us",
    "skip to content",
    # Traditional / Simplified Chinese
    "訂閱",
    "订阅",
    "登入",
    "登录",
    "註冊",
    "注册",
    "會員",
    "会员",
    "隱私權",
    "隐私",
    "服務條款",
    "版權所有",
    "版权所有",
    "廣告",
    "广告",
    "延伸閱讀",
    "相關新聞",
    "相关新闻",
    "熱門新聞",
    "分享",
    "首頁",
    "首页",
    "追蹤我們",
)
_NAVIGATION_PATTERN = re.compile(
    "|".join(
        rf"\b{re.escape(term)}\b" if term.isascii() else re.escape(term)
        for term in sorted(_NAVIGATION_TERMS, key=len, reverse=True)
    ),
    re.IGNORECASE,
)
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "with",
        "from",
        "that",
        "this",
        "its",
        "has",
        "have",
        "was",
        "were",
        "will",
        "into",
        "over",
        "after",
        "about",
        "amid",
        "says",
        "said",
    }
)
_CJK = r"㐀-䶿一-鿿豈-﫿"
_CJK_RUN = re.compile(f"[{_CJK}]+")
_WORD = re.compile(r"[a-z0-9][a-z0-9'\u2019-]{2,}")


@dataclass(frozen=True, slots=True)
class QualityVerdict:
    ok: bool
    reason: str | None = None


def navigation_ratio(text: str) -> float:
    if not text:
        return 0.0
    matched = sum(len(match.group(0)) for match in _NAVIGATION_PATTERN.finditer(text))
    return matched / len(text)


def title_tokens(text: str) -> set[str]:
    lowered = text.lower()
    tokens = {word for word in _WORD.findall(lowered) if word not in _STOPWORDS}
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def title_overlap(title: str, body: str) -> float | None:
    """Share of title tokens found in the body; None when the title is too short to judge."""
    wanted = title_tokens(title)
    if len(wanted) < MIN_TITLE_TOKENS:
        return None
    found = title_tokens(body)
    return len(wanted & found) / len(wanted)


def assess(title: str, body: str) -> QualityVerdict:
    text = " ".join(body.split())
    if len(text) < MIN_BODY_CHARS:
        return QualityVerdict(False, "too_short")
    if navigation_ratio(text) > MAX_NAVIGATION_RATIO:
        return QualityVerdict(False, "navigation_heavy")
    overlap = title_overlap(title, text)
    if overlap is not None and overlap < MIN_TITLE_OVERLAP:
        return QualityVerdict(False, "unrelated_to_title")
    return QualityVerdict(True)
