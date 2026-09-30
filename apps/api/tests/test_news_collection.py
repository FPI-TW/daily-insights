import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from typing import cast

import httpx
import pytest
from pydantic import SecretStr

import daily_insights_api.modules.news.collection as collection
import daily_insights_api.scripts.run_orchestration_worker as orchestration_worker_script
from daily_insights_api.core.config import Settings
from daily_insights_api.modules.news.collection import (
    RETENTION,
    TAIPEI,
    OvernightCollector,
    PollOutcome,
    PollTiming,
    apply_outcome,
    collection_date,
    due_slot,
    merge_sightings,
    poll_due,
    poll_offset,
    poll_slots,
    source_key,
    start_collector,
    supervise_collector,
)
from daily_insights_api.modules.news.feeds import FEED_SOURCES, POLL_GROUPS, FeedSource
from daily_insights_api.modules.news.models import NewsCollectedCandidate, NewsFeedPollState

PATTERN = r"^https://news\.example\.com/a/\d+$"
SOURCE = FeedSource(
    "news.example.com",
    "https://feeds.example.com/rss",
    "rss",
    PATTERN,
    markets=frozenset({"global"}),
    max_items=2,
    display_name="Example",
    poll_group="fast",
)
US_SOURCE = FeedSource(
    "news.example.com",
    "https://feeds.example.com/us.rss",
    "rss",
    PATTERN,
    markets=frozenset({"us_equity"}),
    display_name="Example",
    poll_group="normal",
)
KEYED = FeedSource(
    "news.example.com",
    "https://api.example.com/search?section=world",
    "rss",
    PATTERN,
    display_name="Example",
    api_key_setting="guardian_api_key",
)
NIGHT = date(2026, 9, 30)
# Supervisor and task tests never touch the database.
NO_SESSIONS = cast(collection.Sessions, object())


def taipei(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, second, tzinfo=TAIPEI)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "daily_news_enabled": True,
        "news_collection_enabled": True,
        "news_model_api_key": SecretStr("test-news-model-key"),
        "news_extra_hostnames": "news.example.com",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def rss(*items: tuple[int, datetime | None]) -> bytes:
    body = "".join(
        f"<item><title>Story {number}</title>"
        f"<link>https://news.example.com/a/{number}?utm=rss</link>"
        + (f"<pubDate>{format_datetime(seen.astimezone(UTC))}</pubDate>" if seen else "")
        + "</item>"
        for number, seen in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'.encode()


class FakeStore:
    def __init__(self) -> None:
        self.states: dict[str, NewsFeedPollState] = {}
        self.rows: dict[str, NewsCollectedCandidate] = {}
        self.blocked: frozenset[str] = frozenset()
        self.cleanups: list[datetime] = []

    async def poll_timings(self) -> dict[str, PollTiming]:
        return {
            key: PollTiming(state.last_attempt_at, state.cooldown_until)
            for key, state in self.states.items()
        }

    async def blocked_scopes(self, now: datetime) -> frozenset[str]:
        return self.blocked

    async def validators(self, key: str) -> tuple[str | None, str | None]:
        state = self.states.get(key)
        return (state.etag, state.last_modified) if state is not None else (None, None)

    async def record_poll(
        self, source: FeedSource, outcome: PollOutcome, now: datetime
    ) -> tuple[int, int | None]:
        key = source_key(source)
        state = self.states.setdefault(
            key,
            NewsFeedPollState(
                source_key=key, feed_url=source.url, consecutive_failures=0, gap_count=0
            ),
        )
        gap = apply_outcome(state, outcome, now)
        return len(merge_sightings(self.rows, outcome.candidates, source, now)), gap

    async def cleanup(self, now: datetime) -> int:
        self.cleanups.append(now)
        stale = [key for key, row in self.rows.items() if row.last_collected_at < now - RETENTION]
        for key in stale:
            del self.rows[key]
        return len(stale)


class Harness:
    def __init__(
        self,
        responder: Callable[[httpx.Request], httpx.Response],
        *,
        config: Settings | None = None,
        sources: tuple[FeedSource, ...] = (SOURCE,),
        now: datetime | None = None,
    ) -> None:
        self.now = now or taipei(29, 18, 30)
        self.requests: list[httpx.Request] = []
        self.store = FakeStore()

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return responder(request)

        async def robots(_: httpx.AsyncClient, __: str, ___: frozenset[str]) -> bool:
            return True

        self.collector = OvernightCollector(
            config or settings(),
            self.store,
            clock=lambda: self.now,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            robots=robots,
            sources=sources,
        )

    def state(self, source: FeedSource = SOURCE) -> NewsFeedPollState:
        return self.store.states[source_key(source)]


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    captured: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        collection, "emit_event", lambda name, **fields: captured.append((name, fields))
    )
    return captured


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (taipei(29, 17, 59, 59), None),
        (taipei(29, 18), NIGHT),
        (taipei(29, 23, 59, 59), NIGHT),
        (taipei(30, 0), NIGHT),
        (taipei(30, 7, 59, 59), NIGHT),
        (taipei(30, 8), None),
        # 10:00 UTC is 18:00 in Taipei; the date must not come from UTC.
        (datetime(2026, 9, 29, 10, 0, tzinfo=UTC), NIGHT),
        (datetime(2026, 9, 29, 23, 59, tzinfo=UTC), NIGHT),
    ],
)
def test_collection_date_spans_midnight_in_taipei(moment: datetime, expected: date | None) -> None:
    assert collection_date(moment) == expected


def test_offsets_are_stable_bounded_and_per_feed() -> None:
    offsets = {poll_offset(source) for source in FEED_SOURCES}
    assert all(timedelta(0) <= offset <= timedelta(minutes=10) for offset in offsets)
    assert len(offsets) > len(FEED_SOURCES) // 2
    assert poll_offset(SOURCE) == poll_offset(SOURCE)
    assert poll_offset(SOURCE) != poll_offset(US_SOURCE)


def test_registry_poll_settings_are_valid() -> None:
    for source in FEED_SOURCES:
        assert source.poll_group in POLL_GROUPS
        assert source.poll_interval_minutes is None or source.poll_interval_minutes > 0


def test_slots_cover_first_and_last_poll_for_each_group() -> None:
    offset = poll_offset(SOURCE)
    hourly = poll_slots(SOURCE, NIGHT)
    assert hourly[0] == taipei(29, 18) + offset
    assert hourly[-2] == taipei(30, 7) + offset
    assert hourly[-1] == taipei(30, 7, 40) + offset
    assert len(hourly) == 15
    two_hourly = poll_slots(US_SOURCE, NIGHT)
    assert [slot - poll_offset(US_SOURCE) for slot in two_hourly] == [
        taipei(29, 18),
        taipei(29, 20),
        taipei(29, 22),
        taipei(30, 0),
        taipei(30, 2),
        taipei(30, 4),
        taipei(30, 6),
        taipei(30, 7, 40),
    ]
    custom = FeedSource(**{**SOURCE.__dict__, "poll_interval_minutes": 30})
    assert len(poll_slots(custom, NIGHT)) == 28 + 1


def test_due_slot_respects_offset_last_poll_and_cutoff() -> None:
    offset = poll_offset(SOURCE)
    first = taipei(29, 18) + offset
    last = taipei(30, 7, 40) + offset
    assert due_slot(SOURCE, taipei(29, 17, 59)) is None
    if offset:
        assert due_slot(SOURCE, first - timedelta(seconds=1)) is None
    assert due_slot(SOURCE, first) == first
    # Across midnight the previous evening's slot is still the latest one.
    assert due_slot(SOURCE, taipei(30, 0) + offset - timedelta(seconds=1)) == (
        taipei(29, 23) + offset
    )
    assert due_slot(SOURCE, last) == last
    assert due_slot(SOURCE, taipei(30, 7, 54, 59)) == last
    assert due_slot(SOURCE, taipei(30, 7, 55)) is None
    assert due_slot(SOURCE, taipei(30, 12)) is None


def test_restart_catches_up_only_the_latest_slot() -> None:
    now = taipei(30, 3, 30)
    slot = due_slot(SOURCE, now)
    assert slot is not None
    # The last poll was hours ago; many slots were missed while down.
    stale = PollTiming(last_attempt_at=taipei(29, 19, 5), cooldown_until=None)
    assert poll_due(stale, slot, now)
    state = NewsFeedPollState(source_key=source_key(SOURCE), consecutive_failures=0, gap_count=0)
    apply_outcome(state, PollOutcome(status=200), now)
    caught_up = PollTiming(state.last_attempt_at, state.cooldown_until)
    assert not poll_due(caught_up, due_slot(SOURCE, now + timedelta(minutes=20)), now)
    next_slot = due_slot(SOURCE, taipei(30, 4) + poll_offset(SOURCE))
    assert poll_due(caught_up, next_slot, taipei(30, 4, 11))


async def test_polls_every_item_without_max_items(
    events: list[tuple[str, dict[str, object]]],
) -> None:
    seen = taipei(29, 18, 10)
    harness = Harness(
        lambda _: httpx.Response(200, content=rss((1, seen), (2, seen), (3, seen), (1, seen)))
    )
    harness.now = taipei(29, 18) + poll_offset(SOURCE)

    assert await harness.collector.tick() == 1
    assert len(harness.store.rows) == 3
    state = harness.state()
    assert state.last_status == 200 and state.last_count == 3
    assert state.last_success_at == harness.now
    assert (
        "news.collection.poll",
        {
            "hostname": "news.example.com",
            "feed": SOURCE.url,
            "status": 200,
            "count": 3,
            "new_count": 3,
        },
    ) in events
    # The slot is spent: another tick in the same hour sends nothing.
    harness.now += timedelta(minutes=10)
    assert await harness.collector.tick() == 0
    assert len(harness.requests) == 1


async def test_repeat_sighting_keeps_seen_at_and_unions_markets(
    events: list[tuple[str, dict[str, object]]],
) -> None:
    first_seen = taipei(29, 18, 5)
    payloads = iter([rss((1, first_seen)), rss((1, taipei(29, 21)), (2, taipei(29, 21)))])
    harness = Harness(
        lambda _: httpx.Response(200, content=next(payloads)), sources=(SOURCE, US_SOURCE)
    )
    first = taipei(29, 18, 30)
    harness.now = first
    async with harness.collector.client_factory() as client:
        await harness.collector.poll(client, SOURCE)
        harness.now = taipei(29, 21, 30)
        await harness.collector.poll(client, US_SOURCE)

    rows = {row.url: row for row in harness.store.rows.values()}
    repeat = rows["https://news.example.com/a/1"]
    assert repeat.seen_at == first_seen.astimezone(UTC)
    assert repeat.first_collected_at == first
    assert repeat.last_collected_at == taipei(29, 21, 30)
    assert repeat.markets == ["global", "us_equity"]
    assert repeat.source_key == source_key(SOURCE)
    assert rows["https://news.example.com/a/2"].markets == ["us_equity"]
    assert [fields["new_count"] for name, fields in events if name == "news.collection.poll"] == [
        1,
        1,
    ]


async def test_conditional_request_treats_304_as_success(
    events: list[tuple[str, dict[str, object]]],
) -> None:
    responses = iter(
        [
            httpx.Response(
                200,
                content=rss((1, taipei(29, 18))),
                headers={"ETag": '"v1"', "Last-Modified": "Tue, 29 Sep 2026 10:00:00 GMT"},
            ),
            httpx.Response(304, headers={"ETag": '"v1"'}),
        ]
    )
    harness = Harness(lambda _: next(responses))
    harness.now = taipei(29, 18, 30)
    async with harness.collector.client_factory() as client:
        await harness.collector.poll(client, SOURCE)
        harness.now = taipei(29, 19, 30)
        await harness.collector.poll(client, SOURCE)

    assert harness.requests[1].headers["if-none-match"] == '"v1"'
    assert harness.requests[1].headers["if-modified-since"] == "Tue, 29 Sep 2026 10:00:00 GMT"
    state = harness.state()
    assert state.last_status == 304
    assert state.last_success_at == taipei(29, 19, 30)
    assert state.last_error_code is None and state.consecutive_failures == 0
    assert state.last_count == 1
    assert events[-1][0] == "news.collection.poll" and events[-1][1]["status"] == 304


async def test_429_backs_off_and_honours_longer_retry_after(
    events: list[tuple[str, dict[str, object]]],
) -> None:
    responses = iter(
        [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(429, headers={"Retry-After": "7200"}),
            httpx.Response(200, content=rss((1, taipei(29, 18)))),
        ]
    )
    harness = Harness(lambda _: next(responses))
    start = taipei(29, 18) + poll_offset(SOURCE)
    harness.now = start

    assert await harness.collector.tick() == 1
    state = harness.state()
    assert state.last_status == 503 and state.last_error_code == "source_http_503"
    assert state.consecutive_failures == 1
    assert state.cooldown_until == start + timedelta(minutes=5)
    harness.now = start + timedelta(minutes=4)
    assert await harness.collector.tick() == 0
    # The backoff ends inside the same slot, so the poll is retried.
    harness.now = start + timedelta(minutes=5)
    assert await harness.collector.tick() == 1
    assert state.cooldown_until == harness.now + timedelta(minutes=15)
    harness.now = state.cooldown_until
    assert await harness.collector.tick() == 1
    assert state.last_status == 429 and state.consecutive_failures == 3
    # Retry-After (2h) is longer than the 30-minute backoff and wins.
    assert state.cooldown_until == harness.now + timedelta(hours=2)
    harness.now += timedelta(hours=1, minutes=59)
    assert await harness.collector.tick() == 0
    harness.now += timedelta(minutes=1)
    assert await harness.collector.tick() == 1
    assert state.consecutive_failures == 0 and state.cooldown_until is None
    assert [
        fields["error_code"] for name, fields in events if name == "news.collection.failed"
    ] == [
        "source_http_503",
        "source_http_503",
        "source_http_429",
    ]


async def test_dependency_cooldown_skips_the_source() -> None:
    harness = Harness(lambda _: httpx.Response(200, content=rss()))
    harness.now = taipei(29, 18, 30)
    harness.store.blocked = frozenset({"source:feeds.example.com"})
    assert await harness.collector.tick() == 0
    assert harness.requests == []


async def test_gap_is_reported_within_one_night_and_counted_per_night(
    events: list[tuple[str, dict[str, object]]],
) -> None:
    payloads = iter(
        [
            rss((1, taipei(29, 17))),
            rss((2, taipei(29, 20)), (3, taipei(29, 19, 45))),
            rss((4, taipei(30, 20)), (5, taipei(30, 19, 50))),
        ]
    )
    harness = Harness(lambda _: httpx.Response(200, content=next(payloads)))
    async with harness.collector.client_factory() as client:
        harness.now = taipei(29, 18, 30)
        await harness.collector.poll(client, SOURCE)
        harness.now = taipei(29, 20, 30)
        await harness.collector.poll(client, SOURCE)
        state = harness.state()
        assert state.last_gap_minutes == 75
        assert (state.gap_count_since, state.gap_count) == (NIGHT, 1)
        # The next evening's first poll follows a daytime pause: no gap, and
        # the nightly counter restarts.
        harness.now = taipei(30, 18, 30)
        await harness.collector.poll(client, SOURCE)

    assert (state.gap_count_since, state.gap_count) == (date(2026, 10, 1), 0)
    gaps = [fields for name, fields in events if name == "news.collection.gap"]
    assert gaps == [{"hostname": "news.example.com", "feed": SOURCE.url, "gap_minutes": 75}]


async def test_no_gap_when_the_oldest_item_predates_the_last_success() -> None:
    state = NewsFeedPollState(source_key=source_key(SOURCE), consecutive_failures=0, gap_count=0)
    apply_outcome(state, PollOutcome(status=200), taipei(29, 19))
    outcome = PollOutcome(status=200, oldest_seen_at=taipei(29, 18, 59))
    assert apply_outcome(state, outcome, taipei(29, 20)) is None
    assert state.gap_count == 0 and state.last_gap_minutes is None


async def test_cleanup_drops_rows_older_than_seven_days_hourly(
    events: list[tuple[str, dict[str, object]]],
) -> None:
    harness = Harness(lambda _: httpx.Response(200, content=rss()), now=taipei(29, 12))
    for number, age in ((1, timedelta(days=7, seconds=1)), (2, timedelta(days=6, hours=23))):
        row = NewsCollectedCandidate(
            candidate_id=str(number) * 64,
            url=f"https://news.example.com/a/{number}",
            last_collected_at=harness.now - age,
            first_collected_at=harness.now - age,
            markets=["global"],
        )
        harness.store.rows[row.candidate_id] = row

    await harness.collector.tick()
    assert list(harness.store.rows) == ["2" * 64]
    assert ("news.collection.cleanup", {"deleted": 1}) in events
    harness.now += timedelta(minutes=59)
    await harness.collector.tick()
    harness.now += timedelta(minutes=1)
    await harness.collector.tick()
    assert harness.store.cleanups == [taipei(29, 12), taipei(29, 13)]


@pytest.mark.parametrize(
    "overrides",
    [
        {"news_collection_enabled": False},
        {
            "daily_news_enabled": False,
            "news_collection_enabled": True,
            "news_model_api_key": None,
        },
    ],
)
async def test_disabled_flags_send_no_requests_but_still_clean_up(
    overrides: dict[str, object],
) -> None:
    harness = Harness(lambda _: httpx.Response(200, content=rss()), config=settings(**overrides))
    harness.now = taipei(29, 18, 30)
    assert await harness.collector.tick() == 0
    assert harness.requests == []
    assert harness.store.states == {}
    assert harness.store.cleanups == [harness.now]


async def test_only_allowlisted_hosts_are_polled() -> None:
    harness = Harness(
        lambda _: httpx.Response(200, content=rss()),
        config=settings(news_blocked_hostnames="news.example.com"),
    )
    harness.now = taipei(29, 18, 30)
    assert await harness.collector.tick() == 0
    assert harness.requests == []


async def test_missing_credential_is_recorded_without_a_request(
    events: list[tuple[str, dict[str, object]]],
) -> None:
    harness = Harness(lambda _: httpx.Response(200, content=rss()), sources=(KEYED,))
    harness.now = taipei(29, 18, 30)
    assert await harness.collector.tick() == 1
    assert harness.requests == []
    state = harness.state(KEYED)
    assert state.last_error_code == "source_configuration_missing"
    assert state.cooldown_until is None
    assert events[-1] == (
        "news.collection.failed",
        {
            "hostname": "news.example.com",
            "feed": KEYED.url,
            "status": None,
            "error_code": "source_configuration_missing",
        },
    )


async def test_credentials_never_reach_events(caplog: pytest.LogCaptureFixture) -> None:
    harness = Harness(
        lambda _: httpx.Response(200, content=rss((1, taipei(29, 18)))),
        config=settings(guardian_api_key=SecretStr("very-secret-guardian-key")),
        sources=(KEYED,),
    )
    harness.now = taipei(29, 18, 30)
    with caplog.at_level(logging.INFO, logger="daily_insights"):
        assert await harness.collector.tick() == 1

    assert harness.requests[0].url.params["api-key"] == "very-secret-guardian-key"
    logged = [json.loads(record.getMessage()) for record in caplog.records]
    assert any(item["event"] == "news.collection.poll" for item in logged)
    assert "very-secret-guardian-key" not in caplog.text


async def test_supervisor_restarts_with_backoff_and_never_raises(
    events: list[tuple[str, dict[str, object]]],
) -> None:
    attempts = 0
    delays: list[float] = []

    async def runner(*_: object, **__: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("boom https://user:secret@example.com")

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    clock = iter([taipei(29, 18, minute) for minute in range(10)])
    await supervise_collector(
        settings(),
        NO_SESSIONS,
        clock=lambda: next(clock),
        sleep=sleep,
        runner=runner,
    )

    assert attempts == 3
    assert delays == [30, 60]
    failures = [fields for name, fields in events if name == "news.collection.failed"]
    assert [item["error_code"] for item in failures] == ["unexpected_error"] * 2
    assert all("secret" not in str(item) for item in failures)


async def test_supervisor_propagates_cancellation() -> None:
    started = asyncio.Event()

    async def runner(*_: object, **__: object) -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(supervise_collector(settings(), NO_SESSIONS, runner=runner))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_collector_task_requires_daily_news() -> None:
    disabled = settings(daily_news_enabled=False, news_model_api_key=None)
    assert start_collector(disabled, NO_SESSIONS) is None


async def test_worker_once_mode_never_starts_the_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[bool] = []

    class Heartbeat:
        async def touch(self) -> None:
            return None

    class Engine:
        async def dispose(self) -> None:
            return None

    class Handlers(dict[str, object]):
        async def close(self) -> None:
            return None

    async def nothing(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        orchestration_worker_script, "get_settings", lambda: settings(orchestration_enabled=True)
    )
    monkeypatch.setattr(orchestration_worker_script, "HEARTBEAT_PATH", Heartbeat())
    monkeypatch.setattr(orchestration_worker_script, "create_engine", lambda _: Engine())
    monkeypatch.setattr(orchestration_worker_script, "create_session_factory", lambda _: object())
    monkeypatch.setattr(
        orchestration_worker_script, "build_function_handlers", lambda *_: Handlers()
    )
    monkeypatch.setattr(orchestration_worker_script, "claim_ready_projection", nothing)
    monkeypatch.setattr(orchestration_worker_script, "claim_ready_function", nothing)
    monkeypatch.setattr(orchestration_worker_script, "reconcile_function_jobs", nothing)
    monkeypatch.setattr(
        orchestration_worker_script, "start_collector", lambda *_: started.append(True)
    )

    await orchestration_worker_script.worker_loop(once=True)

    assert started == []


async def test_worker_cancels_the_collector_when_the_loop_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopLoop(Exception):
        pass

    class Heartbeat:
        async def touch(self) -> None:
            return None

    class Engine:
        async def dispose(self) -> None:
            return None

    class Handlers(dict[str, object]):
        async def close(self) -> None:
            return None

    async def nothing(*_: object, **__: object) -> None:
        return None

    async def stop(*_: object, **__: object) -> None:
        raise StopLoop

    collector_task: list[asyncio.Task[None]] = []

    def start(*_: object) -> asyncio.Task[None]:
        task = asyncio.create_task(asyncio.sleep(3600))
        collector_task.append(task)
        return task

    monkeypatch.setattr(
        orchestration_worker_script, "get_settings", lambda: settings(orchestration_enabled=True)
    )
    monkeypatch.setattr(orchestration_worker_script, "HEARTBEAT_PATH", Heartbeat())
    monkeypatch.setattr(orchestration_worker_script, "create_engine", lambda _: Engine())
    monkeypatch.setattr(orchestration_worker_script, "create_session_factory", lambda _: object())
    monkeypatch.setattr(
        orchestration_worker_script, "build_function_handlers", lambda *_: Handlers()
    )
    monkeypatch.setattr(orchestration_worker_script, "claim_ready_projection", nothing)
    monkeypatch.setattr(orchestration_worker_script, "claim_ready_function", nothing)
    monkeypatch.setattr(orchestration_worker_script, "reconcile_function_jobs", stop)
    monkeypatch.setattr(orchestration_worker_script, "start_collector", start)

    with pytest.raises(StopLoop):
        await orchestration_worker_script.worker_loop()

    assert collector_task[0].cancelled()
