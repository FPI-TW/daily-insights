"""Workstream ①: admin-facing source and manual-article operations (spec §6.1).

Owner: ingestion worktree. The admin API (workstream ④) calls only these
functions; each writes ``newsroom_edit_log`` via ``editlog.record_edit``.

Every function works inside the caller's session and only flushes; the caller
commits, so the change and its edit-log row land together. Invalid input
raises ``SourceValidationError`` (a ``ValueError``), a duplicate source key
raises ``SourceConflictError``, and a missing row raises ``LookupError``.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import get_settings
from daily_insights_api.modules.newsroom import editlog, queue
from daily_insights_api.modules.newsroom.ingestion.allowlist import MANUAL_KIND, blocked_hostnames
from daily_insights_api.modules.newsroom.ingestion.feeds import (
    GUARDIAN_API_HOST,
    normalize_manual_url,
    url_hash,
)
from daily_insights_api.modules.newsroom.ingestion.safe_http import (
    UnsafeDestinationError,
    assert_public_hostname,
    normalize_hostname,
    validate_https_url,
)
from daily_insights_api.modules.newsroom.models import (
    BODY_MAX_CHARS,
    MARKET_CODES,
    SOURCE_KINDS,
    NewsroomArticle,
    NewsroomEvent,
    NewsroomSource,
)

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$"
)
_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[a-z]{2,4})?$")


class SourceValidationError(ValueError):
    """The admin's input cannot be stored."""


class SourceConflictError(SourceValidationError):
    """Another source already uses this key."""


@dataclass(frozen=True)
class SourceInput:
    key: str
    name: str
    kind: str
    url: str | None
    hostname: str
    markets: tuple[str, ...]
    trust_tier: int = 2
    weight: float = 1.0
    poll_interval_minutes: int = 30
    enabled: bool = True
    link_pattern: str | None = None
    language_filter: tuple[str, ...] | None = None
    full_text_in_feed: bool = False


_FIELDS = frozenset(item.name for item in fields(SourceInput))


def _validate_field(name: str, value: Any) -> Any:
    """Return the column value for one ``SourceInput`` field, or raise."""
    if name == "key":
        if not isinstance(value, str) or not _KEY.match(value):
            raise SourceValidationError("key must be a lower-case slug of at most 80 characters")
        return value
    if name == "name":
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
            raise SourceValidationError("name must be 1-200 characters")
        return value.strip()
    if name == "kind":
        if value not in SOURCE_KINDS or value == MANUAL_KIND:
            raise SourceValidationError("kind is not a pollable source kind")
        return value
    if name == "url":
        if not isinstance(value, str):
            raise SourceValidationError("url is required")
        try:
            validate_https_url(value.strip(), None)
        except UnsafeDestinationError as error:
            raise SourceValidationError(str(error)) from error
        return value.strip()
    if name == "hostname":
        host = normalize_hostname(value) if isinstance(value, str) else ""
        if not _HOSTNAME.match(host):
            raise SourceValidationError("hostname must be an exact DNS hostname")
        return host
    if name == "markets":
        markets = list(dict.fromkeys(value)) if isinstance(value, list | tuple) else None
        if not markets or any(market not in MARKET_CODES for market in markets):
            raise SourceValidationError("markets must be a non-empty subset of the market codes")
        return markets
    if name == "trust_tier":
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3:
            raise SourceValidationError("trust_tier must be 1-3")
        return value
    if name == "weight":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise SourceValidationError("weight must be a number")
        if not 0.5 <= float(value) <= 2.0:
            raise SourceValidationError("weight must be between 0.5 and 2.0")
        return float(value)
    if name == "poll_interval_minutes":
        if isinstance(value, bool) or not isinstance(value, int) or not 5 <= value <= 1440:
            raise SourceValidationError("poll_interval_minutes must be 5-1440")
        return value
    if name in {"enabled", "full_text_in_feed"}:
        if not isinstance(value, bool):
            raise SourceValidationError(f"{name} must be a boolean")
        return value
    if name == "link_pattern":
        if value is None or value == "":
            return None
        if not isinstance(value, str) or len(value) > 500:
            raise SourceValidationError("link_pattern must be a regular expression")
        try:
            re.compile(value)
        except re.error as error:
            raise SourceValidationError("link_pattern is not a valid regular expression") from error
        return value
    if name == "language_filter":
        if value is None:
            return None
        languages = list(dict.fromkeys(value)) if isinstance(value, list | tuple) else None
        if not languages or any(
            not isinstance(item, str) or not _LANGUAGE.match(item) for item in languages
        ):
            raise SourceValidationError("language_filter must list language codes like en, zh-tw")
        return languages
    raise SourceValidationError(f"unknown source field {name}")


def _snapshot(source: NewsroomSource, names: set[str] | frozenset[str]) -> dict[str, Any]:
    return {name: getattr(source, name) for name in sorted(names)}


def _check_feed_url(url: str | None, kind: str) -> None:
    """Feeds may live off the article host (CDN, feedburner) except the Guardian API,
    which is the only host the Guardian key is ever sent to."""
    if url is None:
        raise SourceValidationError("url is required")
    feed_host = normalize_hostname(urlparse(url).hostname or "")
    if kind == "guardian_api" and feed_host != GUARDIAN_API_HOST:
        raise SourceValidationError(f"guardian_api sources must use {GUARDIAN_API_HOST}")


async def _key_taken(
    database: AsyncSession, key: str, *, other_than: uuid.UUID | None = None
) -> bool:
    query = select(NewsroomSource.id).where(NewsroomSource.key == key)
    if other_than is not None:
        query = query.where(NewsroomSource.id != other_than)
    return (await database.scalar(query)) is not None


async def create_source(
    database: AsyncSession, data: SourceInput, *, user_id: uuid.UUID
) -> uuid.UUID:
    values = {name: _validate_field(name, getattr(data, name)) for name in _FIELDS}
    _check_feed_url(values["url"], values["kind"])
    if await _key_taken(database, values["key"]):
        raise SourceConflictError("a source with this key already exists")
    # next_poll_at stays NULL so the poller picks the new source up right away.
    source = NewsroomSource(**values, options={})
    database.add(source)
    await database.flush()
    editlog.record_edit(
        database,
        entity_type="source",
        entity_id=source.id,
        action="create_source",
        user_id=user_id,
        after=_snapshot(source, _FIELDS),
    )
    await database.flush()
    return source.id


async def update_source(
    database: AsyncSession,
    source_id: uuid.UUID,
    changes: dict[str, object],
    *,
    user_id: uuid.UUID,
) -> None:
    """Partial update of any ``SourceInput`` field; re-enabling resets ``next_poll_at``."""
    unknown = set(changes) - _FIELDS
    if unknown:
        raise SourceValidationError(f"unknown source fields: {sorted(unknown)}")
    source = await database.get(NewsroomSource, source_id, with_for_update=True)
    if source is None:
        raise LookupError("source not found")
    if source.kind == MANUAL_KIND and set(changes) - {"name", "trust_tier", "weight", "markets"}:
        raise SourceValidationError("the manual source only accepts name, tier, weight, markets")
    values = {name: _validate_field(name, value) for name, value in changes.items()}
    if source.kind != MANUAL_KIND:
        _check_feed_url(values.get("url", source.url), values.get("kind", source.kind))
    if "key" in values and await _key_taken(database, values["key"], other_than=source.id):
        raise SourceConflictError("a source with this key already exists")
    changed = {name for name, value in values.items() if getattr(source, name) != value}
    if not changed:
        return
    before = _snapshot(source, changed)
    re_enabled = "enabled" in changed and values["enabled"] is True
    for name in changed:
        setattr(source, name, values[name])
    if re_enabled:
        source.next_poll_at = None
    editlog.record_edit(
        database,
        entity_type="source",
        entity_id=source.id,
        action="update_source",
        user_id=user_id,
        before=before,
        after=_snapshot(source, changed),
    )
    await database.flush()


async def _manual_source(database: AsyncSession) -> NewsroomSource:
    source = await database.scalar(
        select(NewsroomSource).where(
            NewsroomSource.key == MANUAL_KIND, NewsroomSource.kind == MANUAL_KIND
        )
    )
    if source is None:
        raise LookupError("the manual source is missing; run the newsroom seed migration")
    return source


async def submit_manual_url(
    database: AsyncSession,
    url: str,
    *,
    edition_date: date,
    user_id: uuid.UUID,
) -> uuid.UUID:
    """Create a ``manual``-source article queued for fetch → embed → triage.

    The URL must be public HTTPS (validated, and resolved to public addresses
    only); the host need not be a source host because an admin chose it, but
    the blocked-hosts kill switch still applies and the fetch stage still
    honours robots.txt. A URL already stored returns the existing article id.
    The article's title is the URL until the fetch reads the page title, and
    embedding is queued once the fetch settles.
    """
    try:
        validate_https_url(url.strip(), None)
    except UnsafeDestinationError as error:
        raise SourceValidationError(str(error)) from error
    normalized = normalize_manual_url(url)
    host = normalize_hostname(urlparse(normalized).hostname or "")
    if host in blocked_hostnames(get_settings()):
        raise SourceValidationError("this host is blocked")
    try:
        await assert_public_hostname(host, None)
    except UnsafeDestinationError as error:
        raise SourceValidationError(str(error)) from error
    except OSError as error:
        raise SourceValidationError("the host does not resolve") from error
    digest = url_hash(normalized)
    existing = await database.scalar(
        select(NewsroomArticle.id).where(NewsroomArticle.url_hash == digest)
    )
    if existing is not None:
        editlog.record_edit(
            database,
            entity_type="article",
            entity_id=existing,
            action="submit_manual_url",
            user_id=user_id,
            after={"url": normalized, "duplicate": True},
        )
        await database.flush()
        return existing
    source = await _manual_source(database)
    article = NewsroomArticle(
        source_id=source.id,
        url=normalized,
        url_hash=digest,
        title=normalized[:1000],
        first_seen_at=datetime.now(UTC),
        edition_date=edition_date,
        body_status="pending",
        fetch_status="pending",
        embed_status="idle",
        submitted_by_user_id=user_id,
    )
    database.add(article)
    await database.flush()
    editlog.record_edit(
        database,
        entity_type="article",
        entity_id=article.id,
        action="submit_manual_url",
        user_id=user_id,
        after={"url": normalized, "edition_date": edition_date.isoformat()},
    )
    await database.flush()
    return article.id


def _body_digest(body: str | None) -> str | None:
    return hashlib.sha256(body.encode()).hexdigest() if body else None


async def set_manual_body(
    database: AsyncSession, article_id: uuid.UUID, body: str, *, user_id: uuid.UUID
) -> None:
    """Store a pasted body (``body_source = manual``) and unblock a ``needs_body`` event.

    The body is whitespace-trimmed and capped at 40,000 characters. A pending
    fetch is settled as done so it cannot overwrite the pasted text, and a
    manual article still waiting for its embedding is queued for it.
    """
    text = body.strip()[:BODY_MAX_CHARS]
    if not text:
        raise SourceValidationError("body must not be empty")
    article = await database.get(NewsroomArticle, article_id, with_for_update=True)
    if article is None:
        raise LookupError("article not found")
    before = {
        "body_status": article.body_status,
        "body_source": article.body_source,
        "body_chars": len(article.body or ""),
        "body_sha256": _body_digest(article.body),
    }
    article.body = text
    article.body_status = "ok"
    article.body_source = "manual"
    article.body_quality_reason = None
    article.body_fetched_at = datetime.now(UTC)
    if article.fetch_status == "pending":
        article.fetch_status = "done"
        article.fetch_next_attempt_at = None
        article.fetch_error_code = None
    if article.embed_status == "idle":
        article.embed_status = "pending"
        article.embed_next_attempt_at = None
    if article.event_id is not None:
        event = await database.get(NewsroomEvent, article.event_id, with_for_update=True)
        if event is not None and event.analysis_status == "needs_body":
            await queue.enqueue(database, queue.ANALYSIS, [event.id])
    editlog.record_edit(
        database,
        entity_type="article",
        entity_id=article.id,
        action="set_manual_body",
        user_id=user_id,
        before=before,
        after={
            "body_status": "ok",
            "body_source": "manual",
            "body_chars": len(text),
            "body_sha256": _body_digest(text),
        },
    )
    await database.flush()
