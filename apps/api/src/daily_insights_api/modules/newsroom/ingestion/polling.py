"""Source polling: claim due sources, read their feeds, store new articles (spec §6.1).

Each source is polled independently: a failure only touches that source's
health columns. Due sources are claimed with ``FOR UPDATE SKIP LOCKED`` and
leased by pushing ``next_poll_at`` out, so two workers never poll the same
source and a crashed poll is simply retried when the lease lapses.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml import DefusedXmlException
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom import clock
from daily_insights_api.modules.newsroom.ingestion.allowlist import MANUAL_KIND, blocked_hostnames
from daily_insights_api.modules.newsroom.ingestion.deps import IngestionDeps
from daily_insights_api.modules.newsroom.ingestion.feeds import (
    MAX_FEED_BYTES,
    FeedEntry,
    MissingCredentialError,
    SourceConfig,
    build_request,
    entry_language,
    keeps_language,
    parse_feed,
    url_hash,
)
from daily_insights_api.modules.newsroom.ingestion.quality import assess
from daily_insights_api.modules.newsroom.ingestion.safe_http import (
    ResponseTooLargeError,
    RobotsUnavailableError,
    UnsafeDestinationError,
    read_capped,
    robots_allowed,
)
from daily_insights_api.modules.newsroom.models import NewsroomArticle, NewsroomSource
from daily_insights_api.modules.newsroom.notifier import Notice, Notifier

logger = logging.getLogger(__name__)

POLL_LEASE = timedelta(minutes=10)
POLL_BATCH = 32
POLL_CONCURRENCY = 4
# Feeds keep old items around; an item published long before we first saw it
# is not news for this edition (the legacy discovery window was 24 hours).
MAX_ENTRY_AGE = timedelta(hours=24)
UNHEALTHY_AFTER = timedelta(hours=6)
# The admin console's source management page (workstream ④).
SOURCES_ADMIN_PATH = "/admin/newsroom/sources"
# pg_advisory_xact_lock(classid, objid): serialises article inserts per edition
# date so the "same title in the same edition" check cannot race.
_INSERT_LOCK_CLASS = 0x4E520001


class FeedRedirectedError(ValueError):
    """Feeds must not redirect; the registry stores the final URL."""


class RobotsDisallowedError(ValueError):
    pass


@dataclass
class HostThrottle:
    """Minimum spacing between requests to one feed host (Guardian API: 1/s)."""

    _last: dict[str, float] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    async def wait(self, host: str, interval: float) -> None:
        if interval <= 0:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last.get(host, float("-inf"))
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)
            self._last[host] = loop.time()


@dataclass(frozen=True, slots=True)
class PollOutcome:
    source_id: uuid.UUID
    status: str  # ok | failed | skipped
    code: str | None = None
    inserted: int = 0


def poll_error_code(error: BaseException) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"http_{error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.TransportError):
        return "unreachable"
    if isinstance(error, UnsafeDestinationError):
        return "unsafe_destination"
    if isinstance(error, RobotsDisallowedError):
        return "robots_disallowed"
    if isinstance(error, RobotsUnavailableError):
        return "robots_unavailable"
    if isinstance(error, ResponseTooLargeError):
        return "too_large"
    if isinstance(error, FeedRedirectedError):
        return "redirected"
    if isinstance(error, OSError):
        return "dns_failed"
    if isinstance(error, ParseError | DefusedXmlException | json.JSONDecodeError | ValueError):
        return "parse_error"
    return "unexpected_error"


async def claim_due_sources(
    database: AsyncSession, now: datetime, *, limit: int = POLL_BATCH
) -> list[uuid.UUID]:
    """Lease up to ``limit`` due feed sources and commit the lease."""
    due = (
        select(NewsroomSource.id)
        .where(
            NewsroomSource.enabled.is_(True),
            NewsroomSource.kind != MANUAL_KIND,
            or_(NewsroomSource.next_poll_at.is_(None), NewsroomSource.next_poll_at <= now),
        )
        .order_by(NewsroomSource.next_poll_at.asc().nulls_first(), NewsroomSource.key)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    ids = (
        await database.scalars(
            update(NewsroomSource)
            .where(NewsroomSource.id.in_(due.scalar_subquery()))
            .values(next_poll_at=now + POLL_LEASE)
            .returning(NewsroomSource.id)
        )
    ).all()
    await database.commit()
    return list(ids)


async def fetch_feed(
    source: SourceConfig, settings: Settings, deps: IngestionDeps, throttle: HostThrottle
) -> bytes:
    url, headers = build_request(source, settings)
    allowed = frozenset({source.feed_host, source.hostname})
    async with deps.client_factory(allowed, settings.news_discovery_timeout_seconds) as client:
        await throttle.wait(source.feed_host, source.min_interval_seconds)
        if not await robots_allowed(client, url, allowed, resolver=deps.resolver):
            raise RobotsDisallowedError("robots disallow feed")
        async with client.stream("GET", url, headers=headers or None) as response:
            if response.is_redirect:
                raise FeedRedirectedError("feed redirected")
            response.raise_for_status()
            return await read_capped(response, MAX_FEED_BYTES, what="feed")


def _fresh(entry: FeedEntry, now: datetime) -> bool:
    return entry.published_at is None or entry.published_at >= now - MAX_ENTRY_AGE


async def store_entries(
    database: AsyncSession, source: NewsroomSource, entries: Sequence[FeedEntry], now: datetime
) -> int:
    """Insert unseen entries as articles; returns how many rows were created.

    Dedupe: ``url_hash`` is unique (``ON CONFLICT DO NOTHING``), and an entry
    whose title exactly matches an article already in the same edition window
    is skipped. Insert-time queue state follows spec §5.1.
    """
    config = SourceConfig.from_row(source)
    by_hash: dict[str, FeedEntry] = {}
    for entry in entries:
        if _fresh(entry, now):
            by_hash.setdefault(url_hash(entry.url), entry)
    if not by_hash:
        return 0
    known = set(
        (
            await database.scalars(
                select(NewsroomArticle.url_hash).where(NewsroomArticle.url_hash.in_(list(by_hash)))
            )
        ).all()
    )
    fresh: list[tuple[str, FeedEntry, str | None]] = []
    for digest, entry in by_hash.items():
        if digest in known:
            continue
        language = entry_language(entry)
        if keeps_language(language, config.language_filter):
            fresh.append((digest, entry, language))
    if not fresh:
        return 0
    edition_date = clock.edition_date_for(now)
    await database.execute(
        text("SELECT pg_advisory_xact_lock(:class_id, :object_id)"),
        {"class_id": _INSERT_LOCK_CLASS, "object_id": edition_date.toordinal()},
    )
    taken = set(
        (
            await database.scalars(
                select(NewsroomArticle.title).where(
                    NewsroomArticle.edition_date == edition_date,
                    NewsroomArticle.title.in_([entry.title for _, entry, _ in fresh]),
                )
            )
        ).all()
    )
    rows: list[dict[str, Any]] = []
    for digest, entry, language in fresh:
        if entry.title in taken:
            continue
        taken.add(entry.title)
        row: dict[str, Any] = {
            "id": uuid.uuid4(),
            "source_id": source.id,
            "url": entry.url,
            "url_hash": digest,
            "title": entry.title,
            "feed_summary": entry.summary,
            "published_at": entry.published_at,
            "first_seen_at": now,
            "edition_date": edition_date,
            "language": language,
            "body": None,
            "body_status": "pending",
            "body_source": None,
            "body_fetched_at": None,
            "fetch_status": "pending",
            "embed_status": "pending",
        }
        if (
            config.full_text_in_feed
            and entry.body is not None
            and assess(entry.title, entry.body).ok
        ):
            row.update(
                body=entry.body,
                body_status="ok",
                body_source="feed",
                body_fetched_at=now,
                fetch_status="done",
            )
        rows.append(row)
    if not rows:
        return 0
    inserted = await database.scalars(
        insert(NewsroomArticle)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[NewsroomArticle.url_hash])
        .returning(NewsroomArticle.id)
    )
    return len(inserted.all())


def _record_success(source: NewsroomSource, now: datetime) -> None:
    source.last_polled_at = now
    source.last_success_at = now
    source.consecutive_failures = 0
    source.unhealthy_notified_at = None
    source.next_poll_at = now + timedelta(minutes=source.poll_interval_minutes)


def _record_failure(source: NewsroomSource, now: datetime, code: str) -> None:
    source.last_polled_at = now
    source.consecutive_failures += 1
    source.last_error_code = code[:100]
    source.last_error_at = now
    source.next_poll_at = now + timedelta(minutes=source.poll_interval_minutes)


def _record_skip(source: NewsroomSource, now: datetime, code: str) -> None:
    """Configuration gaps are visible to admins but never count as failures."""
    source.last_polled_at = now
    source.last_error_code = code[:100]
    source.last_error_at = now
    source.next_poll_at = now + timedelta(minutes=source.poll_interval_minutes)


async def poll_source(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
    settings: Settings,
    deps: IngestionDeps,
    throttle: HostThrottle,
) -> PollOutcome:
    async with session_factory() as database:
        source = await database.get(NewsroomSource, source_id)
        if source is None:
            return PollOutcome(source_id, "skipped", "missing")
        config = SourceConfig.from_row(source)
        await database.rollback()
        skip_code: str | None = None
        error_code: str | None = None
        entries: list[FeedEntry] = []
        if config.hostname in blocked_hostnames(settings):
            skip_code = "host_blocked"
        else:
            try:
                entries = parse_feed(config, await fetch_feed(config, settings, deps, throttle))
            except MissingCredentialError as error:
                skip_code = error.code
            except Exception as error:
                error_code = poll_error_code(error)
                if error_code == "unexpected_error":
                    logger.exception("newsroom.poll_unexpected", extra={"source": config.key})
        now = deps.clock()
        source = await database.get(NewsroomSource, source_id)
        if source is None:
            return PollOutcome(source_id, "skipped", "missing")
        if skip_code is not None:
            _record_skip(source, now, skip_code)
            await database.commit()
            logger.info("newsroom.poll_skipped", extra={"source": config.key, "code": skip_code})
            return PollOutcome(source_id, "skipped", skip_code)
        if error_code is not None:
            _record_failure(source, now, error_code)
            await database.commit()
            logger.warning("newsroom.poll_failed", extra={"source": config.key, "code": error_code})
            return PollOutcome(source_id, "failed", error_code)
        inserted = await store_entries(database, source, entries, now)
        _record_success(source, now)
        await database.commit()
        logger.info("newsroom.poll_ok", extra={"source": config.key, "inserted": inserted})
        return PollOutcome(source_id, "ok", inserted=inserted)


def sources_admin_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}{SOURCES_ADMIN_PATH}"


async def notify_unhealthy_sources(
    session_factory: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    now: datetime,
    *,
    admin_url: str | None = None,
) -> list[uuid.UUID]:
    """Notify once per outage: failing, and no success for 6 hours (or since creation).

    The marker is claimed in the same statement that selects the source, so
    concurrent workers cannot both notify; a later success clears it.
    """
    async with session_factory() as database:
        rows = (
            await database.execute(
                update(NewsroomSource)
                .where(
                    NewsroomSource.enabled.is_(True),
                    NewsroomSource.consecutive_failures > 0,
                    NewsroomSource.unhealthy_notified_at.is_(None),
                    func.coalesce(NewsroomSource.last_success_at, NewsroomSource.created_at)
                    < now - UNHEALTHY_AFTER,
                )
                .values(unhealthy_notified_at=now)
                .returning(
                    NewsroomSource.id,
                    NewsroomSource.name,
                    NewsroomSource.key,
                    NewsroomSource.consecutive_failures,
                    NewsroomSource.last_error_code,
                    NewsroomSource.last_success_at,
                )
            )
        ).all()
        await database.commit()
    for row in rows:
        last_success = (
            row.last_success_at.astimezone(clock.TAIPEI).strftime("%Y-%m-%d %H:%M")
            if row.last_success_at is not None
            else "從未成功"
        )
        await notifier.send(
            Notice(
                kind="source_unhealthy",
                title=f"新聞來源「{row.name}」持續抓取失敗",
                lines=(
                    f"來源代碼 {row.key}",
                    f"連續失敗 {row.consecutive_failures} 次; 最後錯誤 {row.last_error_code}",
                    f"最後成功 {last_success} (台北時間)",
                ),
                link=admin_url,
                dedupe_key=f"source_unhealthy:{row.id}",
            )
        )
    return [row.id for row in rows]


async def queue_manual_embeddings(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Manual URLs embed after their fetch settles; this also covers exhausted fetches.

    Only never-triaged articles qualify: legacy history imported under the manual
    source is already placed in its events and must not be re-triaged.
    """
    async with session_factory() as database:
        manual_sources = select(NewsroomSource.id).where(NewsroomSource.kind == MANUAL_KIND)
        result = await database.execute(
            update(NewsroomArticle)
            .where(
                and_(
                    NewsroomArticle.source_id.in_(manual_sources.scalar_subquery()),
                    NewsroomArticle.embed_status == "idle",
                    NewsroomArticle.triage_status == "idle",
                    NewsroomArticle.fetch_status.in_(("done", "failed")),
                )
            )
            .values(embed_status="pending", embed_next_attempt_at=None)
        )
        await database.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def run_poll_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    notifier: Notifier,
    deps: IngestionDeps,
    throttle: HostThrottle,
    *,
    concurrency: int = POLL_CONCURRENCY,
) -> list[PollOutcome]:
    """One tick of the every-minute polling task."""
    async with session_factory() as database:
        source_ids = await claim_due_sources(database, deps.clock())
    gate = asyncio.Semaphore(concurrency)

    async def guarded(source_id: uuid.UUID) -> PollOutcome | None:
        async with gate:
            try:
                return await poll_source(session_factory, source_id, settings, deps, throttle)
            except Exception:
                # Database trouble: the lease lapses and the source is retried.
                logger.exception("newsroom.poll_error", extra={"source_id": str(source_id)})
                return None

    outcomes = [item for item in await asyncio.gather(*map(guarded, source_ids)) if item]
    await queue_manual_embeddings(session_factory)
    await notify_unhealthy_sources(
        session_factory,
        notifier,
        deps.clock(),
        admin_url=sources_admin_url(settings.newsroom_admin_base_url),
    )
    return outcomes
