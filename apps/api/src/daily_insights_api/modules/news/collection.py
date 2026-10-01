"""Overnight feed collection: metadata only, polled 18:00-08:00 Asia/Taipei.

A single collector task (guarded by a PostgreSQL session advisory lock) runs
inside the orchestration worker. Each feed is polled on its ``poll_group``
cadence with a fixed per-feed offset; every sighting is upserted into
``news_collected_candidates`` for the 08:00 refresh, and the feed's current
polling health lives in ``news_feed_poll_states``. Article bodies are never
stored.
"""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.news.contracts import Candidate
from daily_insights_api.modules.news.extraction import allowed_hostname, robots_allowed
from daily_insights_api.modules.news.failures import NewsFailure, classify_failure, retry_time
from daily_insights_api.modules.news.feeds import (
    FEED_SOURCES,
    MAX_FEED_BYTES,
    FeedSource,
    _keep_language,
    effective_hostnames,
    feed_client,
    parse_feed,
    request_headers,
    request_url,
)
from daily_insights_api.modules.news.models import (
    NewsCollectedCandidate,
    NewsDependencyState,
    NewsFeedPollState,
)

TAIPEI = ZoneInfo("Asia/Taipei")
Sessions = async_sessionmaker[AsyncSession]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
ClientFactory = Callable[[], httpx.AsyncClient]

FIRST_POLL = time(18)
LAST_POLL = time(7, 40)
# A poll may start no later than this; the 08:00 refresh reads a settled pool.
NO_NEW_POLLS_AFTER = time(7, 55)
COLLECTION_END = time(8)
POLL_INTERVAL_MINUTES = {"flash": 60, "fast": 60, "normal": 120}
MAX_OFFSET_SECONDS = 600
RETENTION = timedelta(days=7)
CLEANUP_INTERVAL = timedelta(hours=1)
TICK_SECONDS = 30.0
# Collector crashes restart after these delays; a run that stayed healthy
# for RESTART_RESET resets the sequence.
RESTART_DELAYS_SECONDS = (30, 60, 120, 300, 600, 900)
RESTART_RESET = timedelta(minutes=10)
COLLECTOR_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"news-overnight-collector").digest()[:8], "big", signed=True
)


def source_key(source: FeedSource) -> str:
    return hashlib.sha256(source.url.encode()).hexdigest()


def feed_hostname(source: FeedSource) -> str:
    return (urlparse(source.url).hostname or "").lower()


def collection_date(now: datetime) -> date | None:
    """The morning (edition) date a collection night belongs to, else None.

    18:00-23:59 belongs to the next day; 00:00-07:59 to the same day.
    """
    local = now.astimezone(TAIPEI)
    if local.time() >= FIRST_POLL:
        return local.date() + timedelta(days=1)
    if local.time() < COLLECTION_END:
        return local.date()
    return None


def poll_offset(source: FeedSource) -> timedelta:
    """Fixed 0-10 minute offset so feeds do not all fire on the hour."""
    return timedelta(seconds=int(source_key(source)[:8], 16) % (MAX_OFFSET_SECONDS + 1))


def poll_interval(source: FeedSource) -> timedelta:
    minutes = source.poll_interval_minutes or POLL_INTERVAL_MINUTES[source.poll_group]
    return timedelta(minutes=minutes)


def poll_slots(source: FeedSource, day: date) -> list[datetime]:
    """Every scheduled poll of the night ending on the morning of ``day``."""
    start = datetime.combine(day - timedelta(days=1), FIRST_POLL, TAIPEI)
    last = datetime.combine(day, LAST_POLL, TAIPEI)
    step, offset = poll_interval(source), poll_offset(source)
    slots: list[datetime] = []
    slot = start
    while slot < last:
        slots.append(slot + offset)
        slot += step
    slots.append(last + offset)
    return slots


def due_slot(source: FeedSource, now: datetime) -> datetime | None:
    """Latest scheduled slot at or before ``now``; None outside the window.

    Only the latest slot is returned, so a restart catches up one poll
    instead of replaying every missed slot.
    """
    day = collection_date(now)
    if day is None:
        return None
    local = now.astimezone(TAIPEI)
    if local.date() == day and local.time() >= NO_NEW_POLLS_AFTER:
        return None
    return max((slot for slot in poll_slots(source, day) if slot <= now), default=None)


@dataclass(frozen=True)
class PollTiming:
    last_attempt_at: datetime | None
    cooldown_until: datetime | None


def poll_due(timing: PollTiming | None, slot: datetime | None, now: datetime) -> bool:
    if slot is None:
        return False
    if timing is None:
        return True
    if timing.cooldown_until is not None and timing.cooldown_until > now:
        return False
    if timing.last_attempt_at is None or timing.last_attempt_at < slot:
        return True
    # A retriable failure already used this slot; retry once its backoff ends.
    return timing.cooldown_until is not None


@dataclass(frozen=True)
class PollOutcome:
    """One feed request's result, independent of storage."""

    status: int | None
    candidates: tuple[Candidate, ...] = ()
    oldest_seen_at: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None
    failure: NewsFailure | None = None


def apply_outcome(state: NewsFeedPollState, outcome: PollOutcome, now: datetime) -> int | None:
    """Update a poll state in place; returns the gap in minutes when detected."""
    previous_success = state.last_success_at
    state.last_attempt_at = now
    state.last_status = outcome.status
    if outcome.failure is not None:
        failures = state.consecutive_failures or 0
        state.last_error_code = outcome.failure.code[:100]
        state.cooldown_until = (
            retry_time(failures, now, outcome.failure.retry_after)
            if outcome.failure.action == "retry"
            else None
        )
        state.consecutive_failures = failures + 1
        return None
    state.last_success_at = now
    state.last_error_code = None
    state.cooldown_until = None
    state.consecutive_failures = 0
    if outcome.etag is not None:
        state.etag = outcome.etag
    if outcome.last_modified is not None:
        state.last_modified = outcome.last_modified
    night = collection_date(now)
    if state.gap_count_since != night:
        state.gap_count_since, state.gap_count = night, 0
    if outcome.status == 304:
        return None
    state.last_count = len(outcome.candidates)
    # Only a gap within the same night is meaningful; the first poll of the
    # evening always starts after a 10-hour daytime pause.
    if (
        previous_success is None
        or outcome.oldest_seen_at is None
        or collection_date(previous_success) != night
        or outcome.oldest_seen_at <= previous_success
    ):
        return None
    gap_minutes = int((outcome.oldest_seen_at - previous_success).total_seconds() // 60)
    state.last_gap_minutes = gap_minutes
    state.gap_count = (state.gap_count or 0) + 1
    return gap_minutes


def merge_sightings(
    existing: dict[str, NewsCollectedCandidate],
    candidates: Iterable[Candidate],
    source: FeedSource,
    now: datetime,
) -> list[NewsCollectedCandidate]:
    """Merge sightings into ``existing`` in place; returns the new rows.

    A repeat sighting only advances ``last_collected_at`` and unions
    ``markets``; ``seen_at`` and ``first_collected_at`` are never rewritten.
    """
    key = source_key(source)
    created: list[NewsCollectedCandidate] = []
    for candidate in candidates:
        row = existing.get(candidate.id)
        if row is not None:
            row.last_collected_at = max(row.last_collected_at, now)
            merged = sorted(set(row.markets) | source.markets)
            if merged != row.markets:
                row.markets = merged
            continue
        row = NewsCollectedCandidate(
            candidate_id=candidate.id,
            url=str(candidate.url),
            hostname=candidate.hostname,
            source_name=candidate.source_name[:100],
            headline=candidate.headline,
            seen_at=candidate.seen_at,
            markets=sorted(source.markets),
            source_key=key,
            first_collected_at=now,
            last_collected_at=now,
        )
        existing[candidate.id] = row
        created.append(row)
    return created


class CollectionStore(Protocol):
    async def poll_timings(self) -> dict[str, PollTiming]: ...

    async def blocked_scopes(self, now: datetime) -> frozenset[str]: ...

    async def validators(self, key: str) -> tuple[str | None, str | None]: ...

    async def record_poll(
        self, source: FeedSource, outcome: PollOutcome, now: datetime
    ) -> tuple[int, int | None]:
        """Persist one poll; returns (new candidate count, gap minutes)."""
        ...

    async def cleanup(self, now: datetime) -> int: ...


class DatabaseCollectionStore:
    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions

    async def poll_timings(self) -> dict[str, PollTiming]:
        async with self.sessions() as database:
            rows = await database.execute(
                select(
                    NewsFeedPollState.source_key,
                    NewsFeedPollState.last_attempt_at,
                    NewsFeedPollState.cooldown_until,
                )
            )
            return {key: PollTiming(attempt, cooldown) for key, attempt, cooldown in rows}

    async def blocked_scopes(self, now: datetime) -> frozenset[str]:
        """Source scopes cooling down in the shared dependency gate (read-only)."""
        async with self.sessions() as database:
            scopes = await database.scalars(
                select(NewsDependencyState.scope).where(
                    NewsDependencyState.scope.like("source:%"),
                    NewsDependencyState.state.not_in(("ready", "no_new_content")),
                    (NewsDependencyState.state.in_(("blocked", "probing")))
                    | (NewsDependencyState.available_at > now),
                )
            )
            return frozenset(scopes)

    async def validators(self, key: str) -> tuple[str | None, str | None]:
        async with self.sessions() as database:
            row = (
                await database.execute(
                    select(NewsFeedPollState.etag, NewsFeedPollState.last_modified).where(
                        NewsFeedPollState.source_key == key
                    )
                )
            ).first()
            return (row[0], row[1]) if row is not None else (None, None)

    async def record_poll(
        self, source: FeedSource, outcome: PollOutcome, now: datetime
    ) -> tuple[int, int | None]:
        key = source_key(source)
        async with self.sessions.begin() as database:
            state = await database.get(NewsFeedPollState, key, with_for_update=True)
            if state is None:
                state = NewsFeedPollState(
                    source_key=key, feed_url=source.url, consecutive_failures=0, gap_count=0
                )
                database.add(state)
            gap_minutes = apply_outcome(state, outcome, now)
            if not outcome.candidates:
                return 0, gap_minutes
            ids = [candidate.id for candidate in outcome.candidates]
            existing = {
                row.candidate_id: row
                for row in await database.scalars(
                    select(NewsCollectedCandidate)
                    .where(NewsCollectedCandidate.candidate_id.in_(ids))
                    .with_for_update()
                )
            }
            created = merge_sightings(existing, outcome.candidates, source, now)
            database.add_all(created)
            return len(created), gap_minutes

    async def cleanup(self, now: datetime) -> int:
        async with self.sessions.begin() as database:
            result = await database.execute(
                delete(NewsCollectedCandidate).where(
                    NewsCollectedCandidate.last_collected_at < now - RETENTION
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)


async def _fetch(
    client: httpx.AsyncClient, url: str, headers: dict[str, str]
) -> tuple[int, httpx.Headers, bytes]:
    """Byte-capped GET that surfaces 304 and validators; errors raise."""
    async with client.stream("GET", url, headers=headers or None) as response:
        # httpx treats every 3xx (including 304) as a redirect.
        if response.status_code == 304:
            return 304, response.headers, b""
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
    return response.status_code, response.headers, b"".join(chunks)


def _qualified(source: FeedSource, payload: bytes) -> tuple[list[Candidate], datetime | None]:
    """Every allowlisted, language-kept item in the bounded response."""
    entries = parse_feed(source, payload)
    dated = [candidate.seen_at for candidate, _ in entries if candidate.seen_at is not None]
    if source.language_filter:
        entries = [
            (candidate, body) for candidate, body in entries if _keep_language(candidate, body)
        ]
    unique: dict[str, Candidate] = {}
    for candidate, _ in entries:
        unique.setdefault(candidate.id, candidate)
    return list(unique.values()), min(dated) if dated else None


class OvernightCollector:
    def __init__(
        self,
        settings: Settings,
        store: CollectionStore,
        *,
        clock: Clock | None = None,
        client_factory: ClientFactory | None = None,
        sleep: Sleep = asyncio.sleep,
        robots: Callable[[httpx.AsyncClient, str, frozenset[str]], Awaitable[bool]] = (
            robots_allowed
        ),
        sources: tuple[FeedSource, ...] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.client_factory = client_factory or (
            lambda: feed_client(settings.news_discovery_timeout_seconds)
        )
        self.sleep = sleep
        self.robots = robots
        self.sources = FEED_SOURCES if sources is None else sources
        self.allowed = effective_hostnames(
            settings.news_extra_hostnames, settings.news_blocked_hostnames
        )
        self.last_cleanup: datetime | None = None
        self.last_request: dict[str, float] = {}

    @property
    def polling_enabled(self) -> bool:
        return self.settings.daily_news_enabled and self.settings.news_collection_enabled

    async def maybe_cleanup(self, now: datetime) -> None:
        if self.last_cleanup is not None and now - self.last_cleanup < CLEANUP_INTERVAL:
            return
        deleted = await self.store.cleanup(now)
        self.last_cleanup = now
        if deleted:
            emit_event("news.collection.cleanup", deleted=deleted)

    async def due_sources(self, now: datetime) -> list[FeedSource]:
        if not self.polling_enabled or collection_date(now) is None:
            return []
        candidates = [
            (source, slot)
            for source in self.sources
            if allowed_hostname(source.hostname, self.allowed)
            and (slot := due_slot(source, now)) is not None
        ]
        if not candidates:
            return []
        timings = await self.store.poll_timings()
        blocked = await self.store.blocked_scopes(now)
        return [
            source
            for source, slot in candidates
            if f"source:{feed_hostname(source)}" not in blocked
            and poll_due(timings.get(source_key(source)), slot, now)
        ]

    async def tick(self) -> int:
        """One scheduler pass; returns the number of feeds polled."""
        now = self.clock()
        await self.maybe_cleanup(now)
        due = await self.due_sources(now)
        if not due:
            return 0
        async with self.client_factory() as client:
            for source in due:
                await self.poll(client, source)
        return len(due)

    async def _space_requests(self, source: FeedSource, host: str) -> None:
        if not source.min_interval_seconds:
            return
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self.last_request.get(host, -1e9)
        if elapsed < source.min_interval_seconds:
            await self.sleep(source.min_interval_seconds - elapsed)
        self.last_request[host] = loop.time()

    async def request(self, client: httpx.AsyncClient, source: FeedSource) -> PollOutcome:
        host = feed_hostname(source)
        now = self.clock()
        url = request_url(source, self.settings)
        headers = request_headers(source, self.settings)
        if url is None or (source.contact_email_setting and headers is None):
            return PollOutcome(
                status=None,
                failure=NewsFailure(
                    code="source_configuration_missing",
                    stage="feed",
                    action="skip",
                    scope=f"source:{host}",
                ),
            )
        conditional = dict(headers or {})
        etag, modified = await self.store.validators(source_key(source))
        if etag:
            conditional["If-None-Match"] = etag
        if modified:
            conditional["If-Modified-Since"] = modified
        try:
            await self._space_requests(source, host)
            if not await self.robots(client, url, self.allowed | {host}):
                raise ValueError("robots disallow feed")
            status, response_headers, payload = await _fetch(client, url, conditional)
            validators = (response_headers.get("etag"), response_headers.get("last-modified"))
            if status == 304:
                return PollOutcome(status=304, etag=validators[0], last_modified=validators[1])
            # Parsing and language detection are CPU-bound; keep the worker's
            # event loop free for function claims.
            candidates, oldest = await asyncio.to_thread(_qualified, source, payload)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = classify_failure(error, stage="feed", scope=f"source:{host}", now=now)
            return PollOutcome(status=failure.http_status, failure=failure)
        return PollOutcome(
            status=status,
            candidates=tuple(candidates),
            oldest_seen_at=oldest,
            etag=validators[0],
            last_modified=validators[1],
        )

    async def poll(self, client: httpx.AsyncClient, source: FeedSource) -> PollOutcome:
        now = self.clock()
        outcome = await self.request(client, source)
        new_count, gap_minutes = await self.store.record_poll(source, outcome, now)
        # ``source.url`` is the registry constant; credentials are only ever
        # added to the per-request URL, which is never logged.
        if outcome.failure is not None:
            emit_event(
                "news.collection.failed",
                hostname=source.hostname,
                feed=source.url,
                status=outcome.status,
                error_code=outcome.failure.code,
            )
            return outcome
        emit_event(
            "news.collection.poll",
            hostname=source.hostname,
            feed=source.url,
            status=outcome.status,
            count=len(outcome.candidates),
            new_count=new_count,
        )
        if gap_minutes is not None:
            emit_event(
                "news.collection.gap",
                hostname=source.hostname,
                feed=source.url,
                gap_minutes=gap_minutes,
            )
        return outcome

    async def run(self, holds_lock: Callable[[], Awaitable[None]] | None = None) -> None:
        while True:
            if holds_lock is not None:
                await holds_lock()
            await self.tick()
            await self.sleep(TICK_SECONDS)


@asynccontextmanager
async def collector_lock(sessions: Sessions) -> AsyncIterator[Callable[[], Awaitable[None]] | None]:
    """Try the collector's session advisory lock on a pinned connection.

    Yields a liveness probe while the lock is held, or None when another
    worker owns the collector. The probe raises when the pinned connection
    (and with it the lock) is gone, so the supervisor restarts and re-locks.
    """
    async with sessions() as database:
        connection = await database.connection()
        acquired = bool(
            (
                await connection.execute(select(func.pg_try_advisory_lock(COLLECTOR_LOCK_KEY)))
            ).scalar()
        )
        await connection.commit()
        if not acquired:
            yield None
            return

        async def probe() -> None:
            await connection.execute(select(1))
            await connection.commit()

        try:
            yield probe
        finally:
            await connection.execute(select(func.pg_advisory_unlock(COLLECTOR_LOCK_KEY)))
            await connection.commit()


async def run_collector(
    settings: Settings,
    sessions: Sessions,
    *,
    clock: Clock | None = None,
    sleep: Sleep = asyncio.sleep,
    client_factory: ClientFactory | None = None,
) -> None:
    """Hold the collector lock and poll until cancelled; standby otherwise."""
    collector = OvernightCollector(
        settings,
        DatabaseCollectionStore(sessions),
        clock=clock,
        client_factory=client_factory,
        sleep=sleep,
    )
    announced = False
    while True:
        async with collector_lock(sessions) as probe:
            if probe is not None:
                emit_event("news.collection.started", polling=collector.polling_enabled)
                await collector.run(probe)
        if not announced:
            emit_event("news.collection.standby")
            announced = True
        await sleep(TICK_SECONDS)


async def supervise_collector(
    settings: Settings,
    sessions: Sessions,
    *,
    clock: Clock | None = None,
    sleep: Sleep = asyncio.sleep,
    runner: Callable[..., Awaitable[None]] = run_collector,
) -> None:
    """Restart the collector with backoff; its failures never reach the worker."""
    now = clock or (lambda: datetime.now(UTC))
    crashes = 0
    while True:
        started = now()
        try:
            await runner(settings, sessions, clock=clock, sleep=sleep)
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if now() - started >= RESTART_RESET:
                crashes = 0
            delay = RESTART_DELAYS_SECONDS[min(crashes, len(RESTART_DELAYS_SECONDS) - 1)]
            crashes += 1
            emit_event(
                "news.collection.failed",
                scope="collector",
                error_code=classify_failure(error, stage="feed").code,
                error_type=type(error).__name__,
                restart_in_seconds=delay,
            )
            await sleep(delay)


def start_collector(settings: Settings, sessions: Sessions) -> asyncio.Task[None] | None:
    """Start the collector task when daily news is enabled.

    With collection disabled the task still runs hourly retention cleanup but
    never sends a feed request.
    """
    if not settings.daily_news_enabled:
        return None
    return asyncio.create_task(supervise_collector(settings, sessions), name="news-collector")
