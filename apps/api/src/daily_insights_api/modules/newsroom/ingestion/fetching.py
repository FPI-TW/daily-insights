"""The ``queue.FETCH`` stage: read the article page, extract and judge its body.

Outcomes (spec §6.1, D18):

- ``ok`` — the body passed ``quality.assess``.
- ``rejected`` — a page was read but the text is not the article.
- ``unavailable`` — robots.txt forbids it, the page is paywalled or access is
  denied, it is not HTML, too large, gone, or redirects off the allowlist.
  This is a final answer, so the stage completes (``fetch_status = done``).
- Only transient network failures (timeouts, DNS, connection errors, 429 and
  5xx) raise ``RetryableStageError`` and go back on the queue.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.ingestion.allowlist import (
    MANUAL_KIND,
    article_allowlist,
    blocked_hostnames,
)
from daily_insights_api.modules.newsroom.ingestion.deps import IngestionDeps
from daily_insights_api.modules.newsroom.ingestion.extract import ExtractedPage, extract_page
from daily_insights_api.modules.newsroom.ingestion.quality import assess
from daily_insights_api.modules.newsroom.ingestion.safe_http import (
    Resolver,
    ResponseTooLargeError,
    RobotsUnavailableError,
    UnsafeDestinationError,
    assert_public_hostname,
    normalize_hostname,
    read_capped,
    robots_allowed,
    validate_https_url,
)
from daily_insights_api.modules.newsroom.models import NewsroomArticle, NewsroomSource
from daily_insights_api.modules.newsroom.queue import Claim, RetryableStageError, StageHandler

logger = logging.getLogger(__name__)

MAX_PAGE_BYTES = 1_500_000
MAX_REDIRECTS = 3
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
ACCESS_DENIED_STATUS = frozenset({401, 402, 403, 451})
GONE_STATUS = frozenset({404, 410})


class ArticleUnavailableError(Exception):
    """A final "cannot read this article" answer; retrying will not change it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    page: ExtractedPage


def _retry_after(response: httpx.Response) -> timedelta | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return timedelta(seconds=max(float(value), 0))
    except ValueError:
        return None


def _status_error(error: httpx.HTTPStatusError) -> Exception:
    status = error.response.status_code
    if status == 429 or status >= 500:
        return RetryableStageError(f"fetch_http_{status}", retry_after=_retry_after(error.response))
    return ArticleUnavailableError(f"http_{status}")


async def _fetch_page(
    client: httpx.AsyncClient,
    url: str,
    allowed: frozenset[str],
    resolver: Resolver | None,
) -> FetchedPage:
    try:
        current = validate_https_url(url, allowed)
    except UnsafeDestinationError as error:
        raise ArticleUnavailableError("host_not_allowed") from error
    for _ in range(MAX_REDIRECTS + 1):
        await assert_public_hostname(urlparse(current).hostname or "", allowed, resolver)
        if not await robots_allowed(client, current, allowed, resolver=resolver):
            raise ArticleUnavailableError("robots_disallowed")
        async with client.stream(
            "GET",
            current,
            follow_redirects=False,
            headers={"Accept": "text/html,application/xhtml+xml"},
        ) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ArticleUnavailableError("redirect_without_location")
                try:
                    current = validate_https_url(urljoin(current, location), allowed)
                except UnsafeDestinationError as error:
                    raise ArticleUnavailableError("redirect_not_allowed") from error
                continue
            if response.status_code in ACCESS_DENIED_STATUS:
                raise ArticleUnavailableError("access_denied")
            if response.status_code in GONE_STATUS:
                raise ArticleUnavailableError("not_found")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            if content_type.strip().lower() not in HTML_TYPES:
                raise ArticleUnavailableError("not_html")
            raw = await read_capped(response, MAX_PAGE_BYTES, what="article response")
        return FetchedPage(current, extract_page(raw.decode("utf-8", errors="replace")))
    raise ArticleUnavailableError("too_many_redirects")


async def fetch_page(
    client: httpx.AsyncClient,
    url: str,
    allowed: frozenset[str],
    *,
    resolver: Resolver | None = None,
) -> FetchedPage:
    """Read one article page, mapping every failure onto unavailable or retryable."""
    try:
        return await _fetch_page(client, url, allowed, resolver)
    except ArticleUnavailableError:
        raise
    except UnsafeDestinationError as error:
        raise ArticleUnavailableError("host_not_public") from error
    except RobotsUnavailableError as error:
        raise ArticleUnavailableError("robots_unavailable") from error
    except ResponseTooLargeError as error:
        raise ArticleUnavailableError("too_large") from error
    except httpx.HTTPStatusError as error:
        raise _status_error(error) from error
    except httpx.TimeoutException as error:
        raise RetryableStageError("fetch_timeout") from error
    except (httpx.TransportError, OSError) as error:
        raise RetryableStageError("fetch_unreachable") from error


def fetch_allowlist(
    allowed: frozenset[str], source_kind: str, article_url: str, settings: Settings
) -> frozenset[str]:
    """Manual URLs were submitted by an admin, so their own host is allowed too.

    The blocked-hosts kill switch still applies to them.
    """
    if source_kind != MANUAL_KIND:
        return allowed
    host = normalize_hostname(urlparse(article_url).hostname or "")
    if not host or host in blocked_hostnames(settings):
        return allowed
    return allowed | {host}


def make_fetch_handler(settings: Settings, deps: IngestionDeps) -> StageHandler:
    async def handler(database: AsyncSession, claim_: Claim) -> dict[str, Any] | None:
        article = await database.get(NewsroomArticle, claim_.row_id)
        if article is None:
            return None
        if article.body_status in {"ok", "purged"}:
            # A body arrived meanwhile (manual paste), or the article aged out.
            return {}
        source = await database.get(NewsroomSource, article.source_id)
        kind = source.kind if source is not None else ""
        allowed = fetch_allowlist(
            await article_allowlist(database, settings), kind, article.url, settings
        )
        now = deps.clock()
        values: dict[str, Any] = {"body_fetched_at": now}
        is_manual = kind == MANUAL_KIND
        if is_manual and article.embed_status == "idle":
            # Manual URLs embed after the fetch so the title comes from the page.
            values["embed_status"] = "pending"
        try:
            async with deps.client_factory(allowed, settings.news_fetch_timeout_seconds) as client:
                fetched = await fetch_page(client, article.url, allowed, resolver=deps.resolver)
        except ArticleUnavailableError as error:
            logger.info(
                "newsroom.fetch_unavailable",
                extra={"article": str(article.id), "reason": error.reason},
            )
            return {
                **values,
                "body": None,
                "body_status": "unavailable",
                "body_quality_reason": error.reason[:100],
            }
        page = fetched.page
        title = article.title
        if is_manual and article.title == article.url and page.title:
            title = page.title
            values["title"] = title
        if article.published_at is None and page.published_at is not None:
            values["published_at"] = page.published_at
        verdict = assess(title, page.text)
        if verdict.ok:
            return {
                **values,
                "body": page.text,
                "body_status": "ok",
                "body_source": "fetch",
                "body_quality_reason": None,
            }
        if page.paywalled:
            return {
                **values,
                "body": None,
                "body_status": "unavailable",
                "body_quality_reason": "paywalled",
            }
        return {
            **values,
            "body": None,
            "body_status": "rejected",
            "body_quality_reason": verdict.reason,
        }

    return handler
