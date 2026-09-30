import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from conftest import remigrate_database
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings
from daily_insights_api.modules.news.collection import (
    DatabaseCollectionStore,
    OvernightCollector,
    PollOutcome,
    collector_lock,
    poll_offset,
    source_key,
)
from daily_insights_api.modules.news.contracts import Candidate
from daily_insights_api.modules.news.failures import NewsFailure
from daily_insights_api.modules.news.feeds import FeedSource
from daily_insights_api.modules.news.models import (
    NewsCollectedCandidate,
    NewsDependencyState,
    NewsFeedPollState,
)

pytestmark = pytest.mark.integration

GLOBAL_FEED = FeedSource(
    "news.example.com",
    "https://feeds.example.com/rss",
    "rss",
    r"^https://news\.example\.com/a/\d+$",
    markets=frozenset({"global"}),
    display_name="Example",
    poll_group="fast",
)
US_FEED = FeedSource(
    "news.example.com",
    "https://feeds.example.com/us.rss",
    "rss",
    r"^https://news\.example\.com/a/\d+$",
    markets=frozenset({"us_equity"}),
    display_name="Example",
)
# 2026-09-29 18:30 Asia/Taipei.
EVENING = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)


def candidate(number: int, seen_at: datetime | None) -> Candidate:
    url = f"https://news.example.com/a/{number}"
    return Candidate(
        id=hashlib.sha256(url.encode()).hexdigest(),
        url=url,
        hostname="news.example.com",
        source_name="Example",
        headline=f"Story {number}",
        seen_at=seen_at,
    )


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required for integration tests")
    remigrate_database(database_url)
    engine = create_async_engine(database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_upsert_keeps_first_sighting_and_unions_markets(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = DatabaseCollectionStore(sessions)
    first_seen = EVENING - timedelta(minutes=20)
    first = PollOutcome(
        status=200,
        candidates=(candidate(1, first_seen), candidate(2, None)),
        oldest_seen_at=first_seen,
        etag='"v1"',
        last_modified="Tue, 29 Sep 2026 10:00:00 GMT",
    )
    assert await store.record_poll(GLOBAL_FEED, first, EVENING) == (2, None)

    later = EVENING + timedelta(hours=2)
    repeat = PollOutcome(
        status=200,
        candidates=(candidate(1, later - timedelta(minutes=5)), candidate(3, later)),
        oldest_seen_at=later - timedelta(minutes=5),
    )
    new_count, gap = await store.record_poll(US_FEED, repeat, later)
    assert new_count == 1 and gap is None

    async with sessions() as database:
        rows = {row.url: row for row in await database.scalars(select(NewsCollectedCandidate))}
        states = {row.source_key: row for row in await database.scalars(select(NewsFeedPollState))}
    kept = rows["https://news.example.com/a/1"]
    assert kept.seen_at == first_seen
    assert kept.first_collected_at == EVENING
    assert kept.last_collected_at == later
    assert kept.markets == ["global", "us_equity"]
    assert kept.source_key == source_key(GLOBAL_FEED)
    assert rows["https://news.example.com/a/2"].seen_at is None
    assert rows["https://news.example.com/a/3"].markets == ["us_equity"]
    state = states[source_key(GLOBAL_FEED)]
    assert (state.feed_url, state.last_status, state.last_count) == (GLOBAL_FEED.url, 200, 2)
    assert (state.etag, state.last_modified) == ('"v1"', "Tue, 29 Sep 2026 10:00:00 GMT")
    assert await store.validators(source_key(GLOBAL_FEED)) == (
        '"v1"',
        "Tue, 29 Sep 2026 10:00:00 GMT",
    )
    assert await store.validators("f" * 64) == (None, None)


async def test_failure_state_gap_counter_and_timings_persist(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = DatabaseCollectionStore(sessions)
    await store.record_poll(GLOBAL_FEED, PollOutcome(status=200), EVENING)
    gap_poll = EVENING + timedelta(hours=1)
    outcome = PollOutcome(
        status=200,
        candidates=(candidate(9, gap_poll),),
        oldest_seen_at=EVENING + timedelta(minutes=40),
    )
    assert await store.record_poll(GLOBAL_FEED, outcome, gap_poll) == (1, 40)
    failed = gap_poll + timedelta(hours=1)
    failure = NewsFailure(
        code="source_http_429",
        stage="feed",
        action="retry",
        scope="source:feeds.example.com",
        http_status=429,
        retry_after=failed + timedelta(hours=1),
    )
    await store.record_poll(GLOBAL_FEED, PollOutcome(status=429, failure=failure), failed)

    async with sessions() as database:
        state = await database.get(NewsFeedPollState, source_key(GLOBAL_FEED))
    assert state is not None
    assert state.last_success_at == gap_poll
    assert state.last_attempt_at == failed
    assert (state.last_status, state.last_error_code) == (429, "source_http_429")
    assert state.consecutive_failures == 1
    assert state.cooldown_until == failed + timedelta(hours=1)
    assert (state.last_gap_minutes, state.gap_count) == (40, 1)
    assert state.gap_count_since is not None and state.gap_count_since.isoformat() == "2026-09-30"
    timings = await store.poll_timings()
    assert timings[source_key(GLOBAL_FEED)].cooldown_until == failed + timedelta(hours=1)


async def test_cleanup_deletes_only_rows_unseen_for_seven_days(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = DatabaseCollectionStore(sessions)
    old = EVENING - timedelta(days=7, seconds=1)
    recent = EVENING - timedelta(days=6, hours=23)
    await store.record_poll(GLOBAL_FEED, PollOutcome(200, (candidate(1, old),)), old)
    await store.record_poll(GLOBAL_FEED, PollOutcome(200, (candidate(2, old),)), old)
    # Re-seen recently: first_collected_at is old but the row stays.
    await store.record_poll(GLOBAL_FEED, PollOutcome(200, (candidate(2, old),)), recent)

    assert await store.cleanup(EVENING) == 1
    async with sessions() as database:
        urls = set(await database.scalars(select(NewsCollectedCandidate.url)))
    assert urls == {"https://news.example.com/a/2"}


async def test_blocked_scopes_read_the_shared_dependency_gate(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as database:
        database.add_all(
            [
                NewsDependencyState(
                    scope="source:cooling.example.com",
                    state="cooldown",
                    available_at=EVENING + timedelta(minutes=5),
                ),
                NewsDependencyState(
                    scope="source:expired.example.com",
                    state="cooldown",
                    available_at=EVENING - timedelta(minutes=5),
                ),
                NewsDependencyState(scope="source:blocked.example.com", state="blocked"),
                NewsDependencyState(scope="source:ready.example.com", state="ready"),
                NewsDependencyState(
                    scope="provider:deepseek",
                    state="cooldown",
                    available_at=EVENING + timedelta(hours=1),
                ),
            ]
        )
    store = DatabaseCollectionStore(sessions)
    assert await store.blocked_scopes(EVENING) == {
        "source:cooling.example.com",
        "source:blocked.example.com",
    }
    async with sessions() as database:
        states = {
            row.scope: row.state for row in await database.scalars(select(NewsDependencyState))
        }
    assert states["source:cooling.example.com"] == "cooldown"


async def test_only_one_collector_holds_the_advisory_lock(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with collector_lock(sessions) as owner:
        assert owner is not None
        await owner()
        async with collector_lock(sessions) as contender:
            assert contender is None
    async with collector_lock(sessions) as successor:
        assert successor is not None


async def test_collector_tick_polls_and_persists_through_the_database(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    payload = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<item><title>Story 1</title><link>https://news.example.com/a/1</link>"
        b"<pubDate>Tue, 29 Sep 2026 10:15:00 GMT</pubDate></item>"
        b"</channel></rss>"
    )

    async def robots(_: httpx.AsyncClient, __: str, ___: frozenset[str]) -> bool:
        return True

    now = EVENING - timedelta(minutes=30) + poll_offset(GLOBAL_FEED)
    collector = OvernightCollector(
        Settings(
            environment="test",
            daily_news_enabled=True,
            news_collection_enabled=True,
            news_model_api_key=SecretStr("test-news-model-key"),
            news_extra_hostnames="news.example.com",
        ),
        DatabaseCollectionStore(sessions),
        clock=lambda: now,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
        ),
        robots=robots,
        sources=(GLOBAL_FEED,),
    )

    assert await collector.tick() == 1
    assert await collector.tick() == 0
    async with sessions() as database:
        row = await database.scalar(select(NewsCollectedCandidate))
        state = await database.get(NewsFeedPollState, source_key(GLOBAL_FEED))
    assert row is not None and row.markets == ["global"]
    assert state is not None and state.last_success_at == now
