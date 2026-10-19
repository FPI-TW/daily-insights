"""Body quality gate: is this extracted text the article, or page chrome? (spec §6.1)

Three checks, in order; the first failure is the ``body_quality_reason``:

1. ``too_short`` — fewer than ``MIN_BODY_CHARS`` characters after whitespace
   collapsing (the legacy extractor's paywall/teaser floor).
2. ``navigation_heavy`` — more than ``MAX_NAVIGATION_RATIO`` of the text is
   navigation / account / sharing boilerplate (menus, cookie banners, consent
   walls that slipped past the ``<nav>``/``<footer>`` stripping), or the text
   reads like a menu: at least ``MIN_MENU_WORDS`` space-separated Chinese
   words and more than ``MAX_MENU_WORD_SHARE`` of them 2 to 4 characters long
   (prose has long runs between spaces).
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
MIN_MENU_WORDS = 10
# Ten days of stored bodies: the 99.9th percentile is 0.78; pure menus are 1.0.
MAX_MENU_WORD_SHARE = 0.9
# A run of at least this many short Chinese words (with short Latin or number
# tokens between them) is a menu, a tag list or a "related" strip...
MIN_MENU_RUN_WORDS = 6
# ...unless it carries more numbers than this, which makes it a flattened table.
MAX_MENU_RUN_NUMBERS = 2

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
_CJK_ANY = re.compile(f"[{_CJK}]")
# A menu label: 2 to 4 Chinese characters. Single characters are left out
# because they are particles ("及", "於") in prose that spaces out Latin words.
_MENU_WORD = re.compile(f"^[{_CJK}]{{2,4}}$")
_MENU_FILLER = re.compile(r"^[A-Za-z0-9]{1,4}$")
_NUMBER = re.compile(r"^\d[\d,.]*$")
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


def strip_menu_runs(text: str) -> str:
    """Drop runs of short menu words (site menus, tag lists) from extracted text."""
    kept: list[str] = []
    run: list[str] = []

    def flush() -> None:
        words = sum(1 for token in run if _MENU_WORD.match(token))
        numbers = sum(1 for token in run if _NUMBER.match(token))
        if words < MIN_MENU_RUN_WORDS or numbers > MAX_MENU_RUN_NUMBERS:
            kept.extend(run)
        run.clear()

    for token in text.split():
        if _MENU_WORD.match(token) or _MENU_FILLER.match(token):
            run.append(token)
        else:
            flush()
            kept.append(token)
    flush()
    return " ".join(kept)


def menu_word_share(text: str) -> float | None:
    """Share of the text's space-separated Chinese words that are menu-length."""
    words = [token for token in text.split() if _CJK_ANY.search(token)]
    if len(words) < MIN_MENU_WORDS:
        return None
    return sum(1 for word in words if _MENU_WORD.match(word)) / len(words)


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
    menu_share = menu_word_share(text)
    if menu_share is not None and menu_share > MAX_MENU_WORD_SHARE:
        return QualityVerdict(False, "navigation_heavy")
    overlap = title_overlap(title, text)
    if overlap is not None and overlap < MIN_TITLE_OVERLAP:
        return QualityVerdict(False, "unrelated_to_title")
    return QualityVerdict(True)
