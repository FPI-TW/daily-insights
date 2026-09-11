"""Candidate discovery: the allowlisted publishers' own feeds and list APIs.

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
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import httpx
from defusedxml import ElementTree
from langdetect import DetectorFactory, LangDetectException, detect
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
    configured_hostnames,
    robots_allowed,
)

MAX_FEED_BYTES = 2_000_000
MAX_PER_FEED = 10
USER_AGENT = "DailyInsightsNewsBot/1.0"
# A feed-supplied body shorter than this is treated as a teaser, not full text.
MIN_FULL_TEXT_CHARS = 200
FEED_KINDS = frozenset({"rss", "rss_full", "news_sitemap", "json_list"})
POLL_GROUPS = frozenset({"flash", "fast", "normal"})
# Languages the summariser handles; feeds flagged ``language_filter`` drop the rest.
KEPT_LANGUAGES = frozenset({"en", "zh-cn", "zh-tw", "ja", "ko"})
GLOBAL = frozenset({GLOBAL_MARKET})
TAIWAN = frozenset({"tw_equity"})
US_AND_GLOBAL = frozenset({GLOBAL_MARKET, "us_equity"})
# The global digest reads English-native sources only, so Chinese, Japanese
# and Korean publishers carry their own (still dormant) market tags and stay
# out of it; those tags pre-sort sources for future editions.
CHINA = frozenset({"cn_equity"})
HONG_KONG = frozenset({"hk_equity"})
JAPAN = frozenset({"jp_equity"})
KOREA = frozenset({"kr_equity"})
# langdetect is non-deterministic unless seeded.
DetectorFactory.seed = 0
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
    # Flash feeds often leave the title empty and carry the text in the body.
    title_fallback_field: str | None = None
    # Flash items are complete in a sentence or two; article feeds need more.
    min_body_chars: int = MIN_FULL_TEXT_CHARS


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
    # Article URLs whose identity lives in the query string (etnet) keep it.
    keep_query: bool = False
    # Zone applied to timestamps the feed publishes without an offset.
    naive_time_zone: str | None = None
    # Newswires mix languages; drop items the summariser cannot handle.
    language_filter: bool = False


_GUARDIAN = JsonListMapping(
    items_path=("response", "results"),
    url_field="webUrl",
    title_field="webTitle",
    time_field="webPublicationDate",
    time_format="iso",
    body_field="fields.bodyText",
)
_GUARDIAN_FIELDS = "order-by=newest&page-size=50&show-fields=bodyText"
_GLOBENEWSWIRE = (
    "https://www.globenewswire.com/RssFeed/subjectcode/{code}-{name}"
    "/feedTitle/GlobeNewswire%20-%20{name}"
)
_GLOBENEWSWIRE_PATTERN = (
    r"^https://www\.globenewswire\.com/news-release/\d{4}/\d{2}/\d{2}/\d+/\d+/[a-z]{2}/.+$"
)
_CNYES_PATTERN = r"^https://news\.cnyes\.com/news/id/\d+$"
_CNBC_PATTERN = r"^https://www\.cnbc\.com/\d{4}/\d{2}/\d{2}/[a-z0-9-]+\.html$"
_GUARDIAN_PATTERN = (
    r"^https://www\.theguardian\.com/[a-z-]+(/[a-z-]+)?/\d{4}/[a-z]{3}/\d{2}/[a-z0-9-]+$"
)
_ETNET_PATTERN = (
    r"^https://www\.etnet\.com\.hk/www/tc/news/home_categorized_news_detail\.php\?newsid=ETN\d+$"
)
_HANKYUNG_PATTERN = r"^https://www\.hankyung\.com/article/\d+[a-z]?$"
_UDN_PATTERN = r"^https://money\.udn\.com/money/story/\d+/\d+$"

# Every source names the article host (``hostname``) explicitly, even when the
# feed lives elsewhere (feedburner, CDN, API hosts), because the allowlist is
# derived from these hostnames. Reuters, BBC and AP are deliberately absent:
# they answer non-browser requests with 401/403 or block crawlers via
# robots.txt, so discovered links could never be extracted; WSJ, MarketWatch,
# Investing.com and Forbes were dropped for the same reason. Verified live on
# 2026-09-03 and 2026-09-04; see docs/architecture/daily-news.md.
FEED_SOURCES: tuple[FeedSource, ...] = (
    # --- Chinese flash APIs (poll_group flash) -------------------------------
    FeedSource(
        "www.cls.cn",
        "https://m.cls.cn/nodeapi/telegraphs?app=CailianpressWap&os=web&sv=1&rn=30",
        "json_list",
        r"^https://www\.cls\.cn/detail/\d+$",
        markets=CHINA,
        display_name="財聯社",
        provides_full_text=True,
        poll_group="flash",
        mapping=JsonListMapping(
            items_path=("data", "roll_data"),
            id_field="id",
            url_template="https://www.cls.cn/detail/{id}",
            title_field="title",
            title_fallback_field="content",
            time_field="ctime",
            time_format="unix_s",
            body_field="content",
            min_body_chars=20,
        ),
    ),
    FeedSource(
        "flash.jin10.com",
        "https://www.jin10.com/flash_newest.js",
        "json_list",
        r"^https://flash\.jin10\.com/detail/\d+$",
        markets=CHINA,
        display_name="金十數據",
        provides_full_text=True,
        poll_group="flash",
        mapping=JsonListMapping(
            items_path=(),
            id_field="id",
            url_template="https://flash.jin10.com/detail/{id}",
            title_field="data.title",
            title_fallback_field="data.content",
            time_field="time",
            time_format="datetime_str",
            body_field="data.content",
            min_body_chars=20,
            js_prefix=True,
        ),
    ),
    FeedSource(
        "wallstreetcn.com",
        "https://api-one.wallstcn.com/apiv1/content/lives?channel=global-channel&client=pc&limit=30",
        "json_list",
        r"^https://wallstreetcn\.com/livenews/\d+$",
        markets=CHINA,
        display_name="華爾街見聞",
        provides_full_text=True,
        poll_group="flash",
        mapping=JsonListMapping(
            items_path=("data", "items"),
            url_field="uri",
            title_field="title",
            title_fallback_field="content_text",
            time_field="display_time",
            time_format="unix_s",
            body_field="content_text",
            min_body_chars=20,
        ),
    ),
    # Without a fresh cache-buster the CDN serves a weeks-old copy with HTTP 200.
    FeedSource(
        "finance.eastmoney.com",
        "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html",
        "json_list",
        r"^https://finance\.eastmoney\.com/a/\d+\.html$",
        markets=CHINA,
        display_name="東方財富",
        poll_group="flash",
        cache_buster_param="r",
        mapping=JsonListMapping(
            items_path=("LivesList",),
            url_field="url_w",
            title_field="title",
            time_field="showtime",
            time_format="datetime_str",
            js_prefix=True,
        ),
    ),
    FeedSource(
        "finance.sina.com.cn",
        "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=30&page=1",
        "json_list",
        r"^https://finance\.sina\.com\.cn/.+\.shtml$",
        markets=CHINA,
        display_name="新浪財經",
        poll_group="flash",
        mapping=JsonListMapping(
            items_path=("result", "data"),
            url_field="url",
            title_field="title",
            time_field="ctime",
            time_format="unix_s",
        ),
    ),
    FeedSource(
        "www.thepaper.cn",
        "https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar",
        "json_list",
        r"^https://www\.thepaper\.cn/newsDetail_forward_\d+$",
        markets=CHINA,
        display_name="澎湃新聞",
        poll_group="flash",
        mapping=JsonListMapping(
            items_path=("data", "hotNews"),
            id_field="contId",
            url_template="https://www.thepaper.cn/newsDetail_forward_{id}",
            title_field="name",
            time_field="pubTimeLong",
            time_format="unix_ms",
        ),
    ),
    # Third-party full-text mirror; the article body arrives in <description>.
    FeedSource(
        "m.jiemian.com",
        "https://feedx.net/rss/jiemian.xml",
        "rss_full",
        r"^https://m\.jiemian\.com/article/\d+\.html$",
        markets=CHINA,
        display_name="界面新聞",
        provides_full_text=True,
        poll_group="flash",
    ),
    # --- Taiwan (poll_group fast) ----------------------------------------------
    # cnyes' content:encoded holds a 300-character teaser, not the article, so
    # these stay plain RSS and go through extraction.
    FeedSource(
        "news.cnyes.com",
        "https://news.cnyes.com/rss/v1/news/category/tw_stock",
        "rss",
        _CNYES_PATTERN,
        markets=TAIWAN,
        max_items=30,
        display_name="鉅亨",
        poll_group="fast",
    ),
    FeedSource(
        "news.cnyes.com",
        "https://news.cnyes.com/rss/v1/news/category/headline",
        "rss",
        _CNYES_PATTERN,
        markets=TAIWAN,
        display_name="鉅亨",
        poll_group="fast",
    ),
    FeedSource(
        "news.cnyes.com",
        "https://news.cnyes.com/rss/v1/news/category/wd_stock",
        "rss",
        _CNYES_PATTERN,
        markets=frozenset({"us_equity"}),
        max_items=20,
        display_name="鉅亨",
        poll_group="fast",
    ),
    FeedSource(
        "money.udn.com",
        "https://money.udn.com/rssfeed/news/1001/5590?ch=money",
        "rss",
        _UDN_PATTERN,
        markets=TAIWAN,
        display_name="經濟日報",
        poll_group="fast",
    ),
    FeedSource(
        "money.udn.com",
        "https://money.udn.com/rssfeed/news/1001/5591?ch=money",
        "rss",
        _UDN_PATTERN,
        markets=TAIWAN,
        display_name="經濟日報",
        poll_group="fast",
    ),
    FeedSource(
        "www.cna.com.tw",
        "https://feeds.feedburner.com/rsscna/finance",
        "rss",
        r"^https://www\.cna\.com\.tw/news/[a-z]+/\d+\.aspx$",
        markets=TAIWAN,
        display_name="中央社",
        poll_group="fast",
    ),
    FeedSource(
        "finance.ettoday.net",
        "https://feeds.feedburner.com/ettoday/finance",
        "rss",
        r"^https://finance\.ettoday\.net/news/\d+$",
        markets=TAIWAN,
        display_name="ETtoday 財經",
        poll_group="fast",
    ),
    # The general TechNews site (climate, gadgets, science) diluted the Taiwan
    # edition; only its finance edition feeds the market.
    FeedSource(
        "finance.technews.tw",
        "https://cdn.technews.tw/feed/",
        "rss",
        r"^https://finance\.technews\.tw/\d{4}/\d{2}/\d{2}/[a-z0-9-]+/$",
        markets=TAIWAN,
        display_name="財經新報",
        poll_group="fast",
    ),
    # The business-only feed lags by days; the site-wide feed is filtered to
    # the business host by the link pattern.
    FeedSource(
        "ec.ltn.com.tw",
        "https://news.ltn.com.tw/rss/all.xml",
        "rss",
        r"^https://ec\.ltn\.com\.tw/article/[a-z]+/\d+$",
        markets=TAIWAN,
        display_name="自由財經",
        poll_group="fast",
    ),
    FeedSource(
        "www.inside.com.tw",
        "https://www.inside.com.tw/feed/rss",
        "rss_full",
        r"^https://www\.inside\.com\.tw/article/\d+-[a-z0-9-]+$",
        markets=TAIWAN,
        display_name="INSIDE",
        provides_full_text=True,
        poll_group="fast",
    ),
    FeedSource(
        "www.gvm.com.tw",
        "https://www.gvm.com.tw/rss",
        "rss",
        r"^https://www\.gvm\.com\.tw/article/\d+$",
        markets=TAIWAN,
        display_name="遠見",
        poll_group="fast",
        naive_time_zone="Asia/Taipei",
    ),
    FeedSource(
        "wantrich.chinatimes.com",
        "https://www.chinatimes.com/sitemaps/sitemap_wantrich_todaynews.xml",
        "news_sitemap",
        r"^https://wantrich\.chinatimes\.com/news/\d+-\d+$",
        markets=TAIWAN,
        max_items=30,
        display_name="旺得富",
        poll_group="fast",
    ),
    FeedSource(
        "www.ctee.com.tw",
        "https://www.ctee.com.tw/sitemaps/sitemap_newstoday.xml",
        "news_sitemap",
        r"^https://www\.ctee\.com\.tw/news/\d+-\d+$",
        markets=TAIWAN,
        max_items=30,
        display_name="工商時報",
        poll_group="fast",
    ),
    FeedSource(
        "www.businesstoday.com.tw",
        "https://www.businesstoday.com.tw/news-sitemap.xml",
        "news_sitemap",
        r"^https://www\.businesstoday\.com\.tw/article/category/\d+/post/\d+/$",
        markets=TAIWAN,
        display_name="今周刊",
        poll_group="fast",
    ),
    FeedSource(
        "www.storm.mg",
        "https://www.storm.mg/feed/sitemap/news",
        "news_sitemap",
        r"^https://www\.storm\.mg/article/\d+$",
        markets=TAIWAN,
        display_name="風傳媒",
        poll_group="fast",
    ),
    # --- Hong Kong, Japan, Korea -------------------------------------------------
    FeedSource(
        "www.etnet.com.hk",
        "https://www.etnet.com.hk/www/tc/news/rss.php?section=editor",
        "rss",
        _ETNET_PATTERN,
        markets=HONG_KONG,
        display_name="經濟通",
        poll_group="fast",
        keep_query=True,
    ),
    FeedSource(
        "www.etnet.com.hk",
        "https://www.etnet.com.hk/www/tc/news/rss.php?section=rumour",
        "rss",
        _ETNET_PATTERN,
        markets=HONG_KONG,
        display_name="經濟通",
        poll_group="fast",
        keep_query=True,
    ),
    FeedSource(
        "www.etnet.com.hk",
        "https://www.etnet.com.hk/www/tc/news/rss.php?section=commentary",
        "rss",
        _ETNET_PATTERN,
        markets=HONG_KONG,
        display_name="經濟通",
        poll_group="fast",
        keep_query=True,
    ),
    FeedSource(
        "www.etnet.com.hk",
        "https://www.etnet.com.hk/www/tc/news/rss.php?section=special",
        "rss",
        _ETNET_PATTERN,
        markets=HONG_KONG,
        display_name="經濟通",
        poll_group="fast",
        keep_query=True,
    ),
    FeedSource(
        "news.rthk.hk",
        "https://rthk9.rthk.hk/rthk/news/rss/c_expressnews_cfinance.xml",
        "rss",
        r"^https://news\.rthk\.hk/rthk/ch/component/k2/\d+-\d+\.htm$",
        markets=HONG_KONG,
        display_name="香港電台",
        poll_group="fast",
    ),
    # Site-wide feed; the pattern keeps the finance, property and China desks.
    FeedSource(
        "www.stheadline.com",
        "https://www.stheadline.com/rss",
        "rss",
        r"^https://www\.stheadline\.com/realtime-(finance|property|china)/\d+/",
        markets=HONG_KONG,
        display_name="星島頭條",
        poll_group="fast",
    ),
    FeedSource(
        "toyokeizai.net",
        "https://toyokeizai.net/list/feed/rss",
        "rss",
        r"^https://toyokeizai\.net/articles/-/\d+$",
        markets=JAPAN,
        display_name="東洋経済",
    ),
    FeedSource(
        "diamond.jp",
        "https://diamond.jp/list/feed/rss/dol",
        "rss",
        r"^https://diamond\.jp/articles/-/\d+$",
        markets=JAPAN,
        display_name="ダイヤモンド",
    ),
    # Mostly press releases under /pr/, which the pattern excludes.
    FeedSource(
        "www.kyodo.co.jp",
        "https://www.kyodo.co.jp/feed/",
        "rss",
        r"^https://www\.kyodo\.co\.jp/(?!pr/)[a-z]+/\d{4}-\d{2}-\d{2}_\d+/$",
        markets=JAPAN,
        display_name="共同通信",
    ),
    # RSS 1.0 mirror of Nikkei's headlines; items are dated with dc:date.
    FeedSource(
        "www.nikkei.com",
        "https://assets.wor.jp/rss/rdf/nikkei/news.rdf",
        "rss",
        r"^https://www\.nikkei\.com/article/[A-Z0-9]+/$",
        markets=JAPAN,
        display_name="日本経済新聞",
    ),
    FeedSource(
        "www.hankyung.com",
        "https://www.hankyung.com/feed/finance",
        "rss",
        _HANKYUNG_PATTERN,
        markets=KOREA,
        display_name="한국경제",
    ),
    FeedSource(
        "www.hankyung.com",
        "https://www.hankyung.com/feed/economy",
        "rss",
        _HANKYUNG_PATTERN,
        markets=KOREA,
        display_name="한국경제",
    ),
    # --- English-native sources for the global digest (verified 2026-09-04:
    # feeds answer 200 and article pages extract with the bot User-Agent) ----
    FeedSource(
        "www.theguardian.com",
        "https://www.theguardian.com/uk/business/rss",
        "rss",
        _GUARDIAN_PATTERN,
        markets=US_AND_GLOBAL,
        max_items=20,
        display_name="The Guardian",
    ),
    FeedSource(
        "www.theguardian.com",
        "https://www.theguardian.com/world/rss",
        "rss",
        _GUARDIAN_PATTERN,
        max_items=20,
        display_name="The Guardian",
    ),
    FeedSource(
        "www.cnbc.com",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "rss",
        _CNBC_PATTERN,
        markets=US_AND_GLOBAL,
        max_items=20,
        display_name="CNBC",
    ),
    FeedSource(
        "www.cnbc.com",
        "https://www.cnbc.com/id/100727362/device/rss/rss.html",
        "rss",
        _CNBC_PATTERN,
        display_name="CNBC",
    ),
    FeedSource(
        "www.cnbc.com",
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",
        "rss",
        _CNBC_PATTERN,
        markets=US_AND_GLOBAL,
        display_name="CNBC",
    ),
    FeedSource(
        "www.cnbc.com",
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",
        "rss",
        _CNBC_PATTERN,
        markets=US_AND_GLOBAL,
        display_name="CNBC",
    ),
    # FX and rates commentary; the feed is macro-only.
    FeedSource(
        "www.fxstreet.com",
        "https://www.fxstreet.com/rss/news",
        "rss",
        r"^https://www\.fxstreet\.com/news/[a-z0-9-]+$",
        max_items=20,
        display_name="FXStreet",
    ),
    # Site-wide feed; the pattern keeps the economy desk only.
    FeedSource(
        "www.aljazeera.com",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "rss",
        r"^https://www\.aljazeera\.com/economy/\d{4}/\d{1,2}/\d{1,2}/[a-z0-9-]+$",
        display_name="Al Jazeera",
    ),
    # Primary sources: central bank releases carry no publish time in the
    # page, so they rank after dated items but are never stale by policy.
    FeedSource(
        "www.federalreserve.gov",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "rss",
        r"^https://www\.federalreserve\.gov/newsevents/pressreleases/[a-z0-9]+\.htm$",
        markets=US_AND_GLOBAL,
        display_name="Federal Reserve",
        max_age_hours=72,
    ),
    FeedSource(
        "www.ecb.europa.eu",
        "https://www.ecb.europa.eu/rss/press.html",
        "rss",
        r"^https://www\.ecb\.europa\.eu/+press/.+\.html$",
        display_name="European Central Bank",
        max_age_hours=72,
    ),
    # /.rss/full/ answers 308 to this feed id; the redirect target is used
    # directly because feed reads refuse redirects.
    FeedSource(
        "www.thestreet.com",
        "https://www.thestreet.com/.rss/feed/a4a58455-5a41-4dfa-899c-86c49b653ed8.xml",
        "rss_full",
        r"^https://www\.thestreet\.com/[a-z-]+/[a-z0-9-]+$",
        markets=US_AND_GLOBAL,
        display_name="TheStreet",
        provides_full_text=True,
    ),
    FeedSource(
        "www.cityam.com",
        "https://www.cityam.com/feed/",
        "rss_full",
        r"^https://www\.cityam\.com/[a-z0-9-]+/$",
        display_name="City A.M.",
        provides_full_text=True,
    ),
    FeedSource(
        "www.globenewswire.com",
        _GLOBENEWSWIRE.format(code=13, name="Earnings%20Releases%20and%20Operating%20Results"),
        "rss",
        _GLOBENEWSWIRE_PATTERN,
        markets=US_AND_GLOBAL,
        display_name="GlobeNewswire",
        poll_group="flash",
        language_filter=True,
    ),
    FeedSource(
        "www.globenewswire.com",
        _GLOBENEWSWIRE.format(code=27, name="Mergers%20and%20Acquisitions"),
        "rss",
        _GLOBENEWSWIRE_PATTERN,
        markets=US_AND_GLOBAL,
        display_name="GlobeNewswire",
        language_filter=True,
    ),
    FeedSource(
        "www.globenewswire.com",
        _GLOBENEWSWIRE.format(code=9, name="Company%20Announcement"),
        "rss",
        _GLOBENEWSWIRE_PATTERN,
        markets=US_AND_GLOBAL,
        display_name="GlobeNewswire",
        language_filter=True,
    ),
    FeedSource(
        "www.prnewswire.com",
        "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
        "rss",
        r"^https://www\.prnewswire\.com/news-releases/[a-z0-9-]+\.html$",
        markets=US_AND_GLOBAL,
        display_name="PR Newswire",
        language_filter=True,
    ),
    # Guardian Content API: 500 calls/day and 1 call/s on the free tier, so
    # the three sections are requested one second apart and skipped entirely
    # while no key is configured.
    FeedSource(
        "www.theguardian.com",
        f"https://content.guardianapis.com/search?section=business&{_GUARDIAN_FIELDS}",
        "json_list",
        _GUARDIAN_PATTERN,
        markets=US_AND_GLOBAL,
        display_name="The Guardian",
        provides_full_text=True,
        mapping=_GUARDIAN,
        api_key_setting="guardian_api_key",
        min_interval_seconds=1.0,
    ),
    FeedSource(
        "www.theguardian.com",
        f"https://content.guardianapis.com/search?section=world&{_GUARDIAN_FIELDS}",
        "json_list",
        _GUARDIAN_PATTERN,
        display_name="The Guardian",
        provides_full_text=True,
        mapping=_GUARDIAN,
        api_key_setting="guardian_api_key",
        min_interval_seconds=1.0,
    ),
    FeedSource(
        "www.theguardian.com",
        f"https://content.guardianapis.com/search?section=politics&{_GUARDIAN_FIELDS}",
        "json_list",
        _GUARDIAN_PATTERN,
        display_name="The Guardian",
        provides_full_text=True,
        mapping=_GUARDIAN,
        api_key_setting="guardian_api_key",
        min_interval_seconds=1.0,
    ),
    # SEC requires a contact address in the User-Agent; the feed is skipped
    # until DAILY_INSIGHTS_SEC_CONTACT_EMAIL is set.
    FeedSource(
        "www.sec.gov",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom",
        "rss",
        r"^https://www\.sec\.gov/Archives/edgar/data/\d+/\d+/[0-9-]+-index\.htm$",
        markets=frozenset({"us_equity"}),
        display_name="SEC EDGAR",
        poll_group="flash",
        contact_email_setting="sec_contact_email",
    ),
)


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
    query = parsed.query if source.keep_query else ""
    normalized = urlunparse(("https", hostname, parsed.path, "", query, ""))
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


def _parse_timestamp(value: str, naive_zone: str | None = None) -> datetime | None:
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


def _html_to_text(html: str) -> str:
    extractor = _ArticleTextExtractor()
    extractor.feed(html)
    return extractor.text()[:MAX_ARTICLE_CHARS]


def parse_rss(payload: bytes, source: FeedSource) -> list[Candidate]:
    """Parse RSS 2.0 and RSS 1.0/RDF items regardless of namespace prefixes."""
    return [candidate for candidate, _ in parse_rss_entries(payload, source)]


def parse_rss_entries(payload: bytes, source: FeedSource) -> list[tuple[Candidate, str | None]]:
    """RSS, RDF and Atom entries with an optional full-text body.

    RSS 2.0 nests ``item`` under ``channel``; RSS 1.0 (RDF) places ``item``
    under the root and dates it with ``dc:date``; Atom uses ``entry`` with
    ``link href`` and ``updated``. Entries are therefore found by local name
    anywhere in the tree. For ``rss_full`` sources the body comes from
    ``content:encoded`` (or ``description`` when a third-party feed inlines
    the article there); bodies shorter than MIN_FULL_TEXT_CHARS are teasers
    (Forbes ships a content:encoded tag holding only a summary) and are
    dropped so extraction fetches the article instead.
    """
    # defusedxml rejects entity expansion and external DTDs; payloads are also
    # byte-capped before reaching the parser.
    root = ElementTree.fromstring(payload)
    result: list[tuple[Candidate, str | None]] = []
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
                str(item.attrib.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", "")),
            )
        title = " ".join(_child_text(item, "title").split())
        seen_at = _parse_timestamp(
            _child_text(item, "pubDate", "date", "published", "updated"), source.naive_time_zone
        )
        url = normalize_article_url(link, source) if link else None
        if url is None or not title:
            continue
        body: str | None = None
        if source.kind == "rss_full":
            raw = _child_text(item, "encoded") or _child_text(item, "description")
            text = _html_to_text(raw) if raw else ""
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
        seen_at = _parse_timestamp(published, source.naive_time_zone) if published else None
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
        if not title and mapping.title_fallback_field:
            fallback = _lookup(item, mapping.title_fallback_field)
            if isinstance(fallback, str):
                title = " ".join(_html_to_text(fallback).split())[:200]
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
                    text_body[:MAX_ARTICLE_CHARS]
                    if len(text_body) >= mapping.min_body_chars
                    else None
                )
        result.append((_candidate(url, title, seen_at, source), body))
    return result


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
    raise ValueError(f"unknown feed kind {source.kind}")


def detected_language(text: str) -> str | None:
    try:
        return str(detect(text))
    except LangDetectException:
        return None


def _keep_language(candidate: Candidate, body: str | None) -> bool:
    sample = candidate.headline if body is None else f"{candidate.headline} {body[:500]}"
    language = detected_language(sample)
    # Undetectable text (numbers, tickers) is kept; only a confident foreign
    # language drops an item.
    return language is None or language in KEPT_LANGUAGES


def registry_hostnames() -> frozenset[str]:
    return frozenset(source.hostname for source in FEED_SOURCES)


def effective_hostnames(extra: str = "", blocked: str = "") -> frozenset[str]:
    """Allowlist derived from the registry, plus ``extra`` minus ``blocked``.

    Both overrides are comma-separated exact hostnames; blocking a registry
    host silently disables its feeds, which is the intended kill switch.
    """
    allowed = set(registry_hostnames())
    if extra.strip():
        allowed |= configured_hostnames(extra)
    if blocked.strip():
        allowed -= configured_hostnames(blocked)
    return frozenset(allowed)


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
    # Local import avoids coupling the pure feed parsers to model execution.
    from daily_insights_api.modules.news.failures import NewsFailure, NewsOperationError
    from daily_insights_api.modules.news.recovery import (
        check_dependency,
        current_workflow,
        fingerprint,
        source_failure,
        source_success,
    )

    workflow = current_workflow()
    if workflow is not None:
        workflow.progress["feeds_ok"] = 0
    start = end - timedelta(hours=24)
    resolved_settings = settings or get_settings()
    result: list[Candidate] = []
    last_request: dict[str, float] = {}
    for source in FEED_SOURCES:
        if market not in source.markets or not allowed_hostname(source.hostname, allowed):
            continue
        checkpoint = None
        feed_host = (urlparse(source.url).hostname or "").lower()
        if workflow is not None:
            checkpoint = await workflow.checkpoint(fingerprint(["feed", source.url]), "feed")
            if checkpoint.failure is not None and checkpoint.failure.get("action") == "skip":
                await workflow.record(NewsFailure.model_validate(checkpoint.failure))
                continue
            if checkpoint.result is not None and not source.provides_full_text:
                workflow.progress["feeds_ok"] += 1
                result.extend(
                    Candidate.model_validate(item) for item in checkpoint.result["candidates"]
                )
                continue
        url = request_url(source, resolved_settings)
        if url is None:
            if workflow is not None and checkpoint is not None:
                await source_failure(
                    workflow,
                    checkpoint,
                    NewsOperationError(
                        NewsFailure(
                            code="source_configuration_missing",
                            stage="feed",
                            action="skip",
                            scope=f"source:{feed_host}",
                        )
                    ),
                    stage="feed",
                    hostname=feed_host,
                )
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
            if workflow is not None and checkpoint is not None:
                await source_failure(
                    workflow,
                    checkpoint,
                    NewsOperationError(
                        NewsFailure(
                            code="source_configuration_missing",
                            stage="feed",
                            action="skip",
                            scope=f"source:{feed_host}",
                        )
                    ),
                    stage="feed",
                    hostname=feed_host,
                )
            emit_event(
                "news.feed.skipped",
                hostname=source.hostname,
                feed=source.url,
                reason="missing_contact_email",
            )
            continue
        try:
            if workflow is not None:
                await workflow.check("feed")
                await check_dependency(workflow, f"source:{feed_host}")
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
            if workflow is not None and checkpoint is not None:
                await source_failure(workflow, checkpoint, error, stage="feed", hostname=feed_host)
            emit_event(
                "news.feed.failed",
                hostname=source.hostname,
                feed=source.url,
                error_code=type(error).__name__,
            )
            continue
        dropped_language = 0
        if source.language_filter:
            readable = [
                (candidate, body) for candidate, body in entries if _keep_language(candidate, body)
            ]
            dropped_language = len(entries) - len(readable)
            entries = readable
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
        if workflow is not None and checkpoint is not None:
            workflow.progress["feeds_ok"] += 1
            checkpoint.result = {
                "candidates": [candidate.model_dump(mode="json") for candidate in found]
            }
            checkpoint.failure = None
            await workflow.store(checkpoint)
            await source_success(workflow, f"source:{feed_host}", newest, len(found))
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
            dropped_language=dropped_language,
        )
        result.extend(found)
    return _dedupe_candidates(result)
