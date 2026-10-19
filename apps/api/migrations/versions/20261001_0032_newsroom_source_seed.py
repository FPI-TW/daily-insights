"""Seed newsroom_sources from the legacy feed registry, plus the manual source.

Revision ID: 20261001_0032
Revises: 20261001_0031

Data only. Rows mirror ``modules/news/feeds.py::FEED_SOURCES`` as of
2026-10-01 (32 feeds): legacy ``markets`` map onto the newsroom market codes,
``poll_group`` becomes ``poll_interval_minutes`` (flash 10, fast 15, normal 30),
``provides_full_text`` becomes ``full_text_in_feed``, the language flag becomes
the kept-language list, and adapter parameters move into ``options``. The
Guardian Content API feeds become ``guardian_api`` sources (key from
``guardian_api_key``) and the SEC EDGAR Atom feed needs ``sec_contact_email``.
"""

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261001_0032"
down_revision: str | None = "20261001_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMESPACE = uuid.UUID("5b0c6d8e-2f4a-4b8e-9a51-6e1f0d3c7a20")
GLOBAL = ["global"]
TAIWAN = ["tw_equity"]
US = ["us_equity"]
US_AND_GLOBAL = ["global", "us_equity"]
KEPT_LANGUAGES = ["en", "zh-cn", "zh-tw", "ja", "ko"]
FLASH, FAST, NORMAL = 10, 15, 30

_CNYES = r"^https://news\.cnyes\.com/news/id/\d+$"
_UDN = r"^https://money\.udn\.com/money/story/\d+/\d+$"
_CNBC = r"^https://www\.cnbc\.com/\d{4}/\d{2}/\d{2}/[a-z0-9-]+\.html$"
_GUARDIAN = r"^https://www\.theguardian\.com/[a-z-]+(/[a-z-]+)?/\d{4}/[a-z]{3}/\d{2}/[a-z0-9-]+$"
_GLOBENEWSWIRE = (
    r"^https://www\.globenewswire\.com/news-release/\d{4}/\d{2}/\d{2}/\d+/\d+/[a-z]{2}/.+$"
)
_GLOBENEWSWIRE_FEED = (
    "https://www.globenewswire.com/RssFeed/subjectcode/{code}-{name}"
    "/feedTitle/GlobeNewswire%20-%20{name}"
)
_GUARDIAN_API = (
    "https://content.guardianapis.com/search?section={section}"
    "&order-by=newest&page-size=50&show-fields=bodyText,trailText"
)


def _source(
    key: str,
    name: str,
    kind: str,
    url: str | None,
    hostname: str,
    link_pattern: str | None,
    markets: list[str],
    trust_tier: int,
    *,
    poll: int = NORMAL,
    full_text: bool = False,
    language_filter: list[str] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid5(_NAMESPACE, key),
        "key": key,
        "name": name,
        "kind": kind,
        "url": url,
        "hostname": hostname,
        "link_pattern": link_pattern,
        "markets": markets,
        "language_filter": language_filter,
        "full_text_in_feed": full_text,
        "options": options or {},
        "trust_tier": trust_tier,
        "weight": 1.0,
        "poll_interval_minutes": poll,
        "enabled": True,
    }


# trust_tier: 3 = central bank, regulator, exchange or official press release;
# 2 = national wire or major financial / news outlet; 1 = everything else.
SOURCES: tuple[dict[str, Any], ...] = (
    # --- Taiwan ------------------------------------------------------------------
    _source(
        "cnyes-tw-stock",
        "鉅亨",
        "rss",
        "https://news.cnyes.com/rss/v1/news/category/tw_stock",
        "news.cnyes.com",
        _CNYES,
        TAIWAN,
        2,
        poll=FAST,
    ),
    _source(
        "cnyes-headline",
        "鉅亨",
        "rss",
        "https://news.cnyes.com/rss/v1/news/category/headline",
        "news.cnyes.com",
        _CNYES,
        TAIWAN,
        2,
        poll=FAST,
    ),
    _source(
        "cnyes-wd-stock",
        "鉅亨",
        "rss",
        "https://news.cnyes.com/rss/v1/news/category/wd_stock",
        "news.cnyes.com",
        _CNYES,
        US,
        2,
        poll=FAST,
    ),
    _source(
        "udn-money-5590",
        "經濟日報",
        "rss",
        "https://money.udn.com/rssfeed/news/1001/5590?ch=money",
        "money.udn.com",
        _UDN,
        TAIWAN,
        2,
        poll=FAST,
    ),
    _source(
        "udn-money-5591",
        "經濟日報",
        "rss",
        "https://money.udn.com/rssfeed/news/1001/5591?ch=money",
        "money.udn.com",
        _UDN,
        TAIWAN,
        2,
        poll=FAST,
    ),
    _source(
        "cna-finance",
        "中央社",
        "rss",
        "https://feeds.feedburner.com/rsscna/finance",
        "www.cna.com.tw",
        r"^https://www\.cna\.com\.tw/news/[a-z]+/\d+\.aspx$",
        TAIWAN,
        2,
        poll=FAST,
    ),
    _source(
        "ettoday-finance",
        "ETtoday 財經",
        "rss",
        "https://feeds.feedburner.com/ettoday/finance",
        "finance.ettoday.net",
        r"^https://finance\.ettoday\.net/news/\d+$",
        TAIWAN,
        1,
        poll=FAST,
    ),
    _source(
        "technews-finance",
        "財經新報",
        "rss",
        "https://cdn.technews.tw/feed/",
        "finance.technews.tw",
        r"^https://finance\.technews\.tw/\d{4}/\d{2}/\d{2}/[a-z0-9-]+/$",
        TAIWAN,
        1,
        poll=FAST,
    ),
    _source(
        "ltn-business",
        "自由財經",
        "rss",
        "https://news.ltn.com.tw/rss/all.xml",
        "ec.ltn.com.tw",
        r"^https://ec\.ltn\.com\.tw/article/[a-z]+/\d+$",
        TAIWAN,
        2,
        poll=FAST,
    ),
    _source(
        "inside",
        "INSIDE",
        "rss_full",
        "https://www.inside.com.tw/feed/rss",
        "www.inside.com.tw",
        r"^https://www\.inside\.com\.tw/article/\d+-[a-z0-9-]+$",
        TAIWAN,
        1,
        poll=FAST,
        full_text=True,
    ),
    _source(
        "gvm",
        "遠見",
        "rss",
        "https://www.gvm.com.tw/rss",
        "www.gvm.com.tw",
        r"^https://www\.gvm\.com\.tw/article/\d+$",
        TAIWAN,
        2,
        poll=FAST,
        options={"naive_time_zone": "Asia/Taipei"},
    ),
    _source(
        "businesstoday",
        "今周刊",
        "news_sitemap",
        "https://www.businesstoday.com.tw/news-sitemap.xml",
        "www.businesstoday.com.tw",
        r"^https://www\.businesstoday\.com\.tw/article/category/\d+/post/\d+/$",
        TAIWAN,
        2,
        poll=FAST,
    ),
    _source(
        "storm",
        "風傳媒",
        "news_sitemap",
        "https://www.storm.mg/feed/sitemap/news",
        "www.storm.mg",
        r"^https://www\.storm\.mg/article/\d+$",
        TAIWAN,
        1,
        poll=FAST,
    ),
    # --- Global / US -------------------------------------------------------------
    _source(
        "guardian-business-rss",
        "The Guardian",
        "rss",
        "https://www.theguardian.com/uk/business/rss",
        "www.theguardian.com",
        _GUARDIAN,
        US_AND_GLOBAL,
        2,
    ),
    _source(
        "guardian-world-rss",
        "The Guardian",
        "rss",
        "https://www.theguardian.com/world/rss",
        "www.theguardian.com",
        _GUARDIAN,
        GLOBAL,
        2,
    ),
    _source(
        "cnbc-top-news",
        "CNBC",
        "rss",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "www.cnbc.com",
        _CNBC,
        US_AND_GLOBAL,
        2,
    ),
    _source(
        "cnbc-world",
        "CNBC",
        "rss",
        "https://www.cnbc.com/id/100727362/device/rss/rss.html",
        "www.cnbc.com",
        _CNBC,
        GLOBAL,
        2,
    ),
    _source(
        "cnbc-economy",
        "CNBC",
        "rss",
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",
        "www.cnbc.com",
        _CNBC,
        US_AND_GLOBAL,
        2,
    ),
    _source(
        "cnbc-finance",
        "CNBC",
        "rss",
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",
        "www.cnbc.com",
        _CNBC,
        US_AND_GLOBAL,
        2,
    ),
    _source(
        "fxstreet-news",
        "FXStreet",
        "rss",
        "https://www.fxstreet.com/rss/news",
        "www.fxstreet.com",
        r"^https://www\.fxstreet\.com/news/[a-z0-9-]+$",
        GLOBAL,
        1,
    ),
    _source(
        "aljazeera-economy",
        "Al Jazeera",
        "rss",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "www.aljazeera.com",
        r"^https://www\.aljazeera\.com/economy/\d{4}/\d{1,2}/\d{1,2}/[a-z0-9-]+$",
        GLOBAL,
        2,
    ),
    _source(
        "fed-press",
        "Federal Reserve",
        "rss",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "www.federalreserve.gov",
        r"^https://www\.federalreserve\.gov/newsevents/pressreleases/[a-z0-9]+\.htm$",
        US_AND_GLOBAL,
        3,
    ),
    _source(
        "ecb-press",
        "European Central Bank",
        "rss",
        "https://www.ecb.europa.eu/rss/press.html",
        "www.ecb.europa.eu",
        r"^https://www\.ecb\.europa\.eu/+press/.+\.html$",
        GLOBAL,
        3,
    ),
    _source(
        "thestreet",
        "TheStreet",
        "rss_full",
        "https://www.thestreet.com/.rss/feed/a4a58455-5a41-4dfa-899c-86c49b653ed8.xml",
        "www.thestreet.com",
        r"^https://www\.thestreet\.com/[a-z-]+/[a-z0-9-]+$",
        US_AND_GLOBAL,
        1,
        full_text=True,
    ),
    _source(
        "cityam",
        "City A.M.",
        "rss_full",
        "https://www.cityam.com/feed/",
        "www.cityam.com",
        r"^https://www\.cityam\.com/[a-z0-9-]+/$",
        GLOBAL,
        1,
        full_text=True,
    ),
    _source(
        "globenewswire-earnings",
        "GlobeNewswire",
        "rss",
        _GLOBENEWSWIRE_FEED.format(code=13, name="Earnings%20Releases%20and%20Operating%20Results"),
        "www.globenewswire.com",
        _GLOBENEWSWIRE,
        US_AND_GLOBAL,
        3,
        poll=FLASH,
        language_filter=KEPT_LANGUAGES,
    ),
    _source(
        "globenewswire-mna",
        "GlobeNewswire",
        "rss",
        _GLOBENEWSWIRE_FEED.format(code=27, name="Mergers%20and%20Acquisitions"),
        "www.globenewswire.com",
        _GLOBENEWSWIRE,
        US_AND_GLOBAL,
        3,
        language_filter=KEPT_LANGUAGES,
    ),
    _source(
        "prnewswire-financial-services",
        "PR Newswire",
        "rss",
        "https://www.prnewswire.com/rss/financial-services-latest-news/"
        "financial-services-latest-news-list.rss",
        "www.prnewswire.com",
        r"^https://www\.prnewswire\.com/news-releases/[a-z0-9-]+\.html$",
        US_AND_GLOBAL,
        3,
        language_filter=KEPT_LANGUAGES,
    ),
    # Guardian Content API: 500 calls/day and 1 call/s on the free tier.
    _source(
        "guardian-api-business",
        "The Guardian",
        "guardian_api",
        _GUARDIAN_API.format(section="business"),
        "www.theguardian.com",
        _GUARDIAN,
        US_AND_GLOBAL,
        2,
        full_text=True,
        options={"min_interval_seconds": 1.0},
    ),
    _source(
        "guardian-api-world",
        "The Guardian",
        "guardian_api",
        _GUARDIAN_API.format(section="world"),
        "www.theguardian.com",
        _GUARDIAN,
        GLOBAL,
        2,
        full_text=True,
        options={"min_interval_seconds": 1.0},
    ),
    _source(
        "guardian-api-politics",
        "The Guardian",
        "guardian_api",
        _GUARDIAN_API.format(section="politics"),
        "www.theguardian.com",
        _GUARDIAN,
        GLOBAL,
        2,
        full_text=True,
        options={"min_interval_seconds": 1.0},
    ),
    # SEC EDGAR requires a contact address in the User-Agent.
    _source(
        "sec-8k",
        "SEC EDGAR",
        "atom",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom",
        "www.sec.gov",
        r"^https://www\.sec\.gov/Archives/edgar/data/\d+/\d+/[0-9-]+-index\.htm$",
        US,
        3,
        poll=FLASH,
        options={"requires_contact_email": True},
    ),
    # Off-pool URLs submitted by admins; never polled.
    _source(
        "manual",
        "手動輸入",
        "manual",
        None,
        "manual.invalid",
        None,
        ["global", "tw_equity", "us_equity"],
        2,
    ),
)

_TABLE = sa.table(
    "newsroom_sources",
    sa.column("id", sa.UUID()),
    sa.column("key", sa.String()),
    sa.column("name", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("url", sa.Text()),
    sa.column("hostname", sa.String()),
    sa.column("link_pattern", sa.String()),
    sa.column("markets", postgresql.ARRAY(sa.String())),
    sa.column("language_filter", postgresql.ARRAY(sa.String())),
    sa.column("full_text_in_feed", sa.Boolean()),
    sa.column("options", postgresql.JSONB()),
    sa.column("trust_tier", sa.Integer()),
    sa.column("weight", sa.Float()),
    sa.column("poll_interval_minutes", sa.Integer()),
    sa.column("enabled", sa.Boolean()),
)


def upgrade() -> None:
    op.bulk_insert(_TABLE, list(SOURCES))


def downgrade() -> None:
    op.execute(_TABLE.delete().where(_TABLE.c.key.in_([source["key"] for source in SOURCES])))
