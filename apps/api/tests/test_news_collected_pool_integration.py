import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from conftest import remigrate_database
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import daily_insights_api.modules.orchestration.news_functions as orchestration_news_functions
from daily_insights_api.core.config import Settings
from daily_insights_api.modules.news.collected_pool import load_collected_candidates
from daily_insights_api.modules.news.contracts import Candidate, Selection
from daily_insights_api.modules.news.extraction import FetchedCandidate
from daily_insights_api.modules.news.llm import ModelCall
from daily_insights_api.modules.news.models import (
    NewsCandidate,
    NewsCandidateBatch,
    NewsCollectedCandidate,
)
from daily_insights_api.modules.news.service import ExtractionOutcome
from daily_insights_api.modules.orchestration.models import FunctionRun, JobRun
from daily_insights_api.modules.orchestration.service import LEASE_DURATION
from daily_insights_api.modules.orchestration.worker import FunctionOutcome, claim_ready_function

pytestmark = pytest.mark.integration

ALLOWED = frozenset({"pool.example", "live.example"})


@pytest_asyncio.fixture
async def news_database() -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    remigrate_database(database_url)
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, sessions
    finally:
        await engine.dispose()


def _candidate_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _pool_row(
    slug: str,
    *,
    seen_at: datetime | None,
    first_collected_at: datetime,
    markets: tuple[str, ...] = ("global",),
    hostname: str = "pool.example",
    headline: str | None = None,
) -> NewsCollectedCandidate:
    url = f"https://{hostname}/{slug}"
    return NewsCollectedCandidate(
        candidate_id=_candidate_id(url),
        url=url,
        hostname=hostname,
        source_name="Pool Wire",
        headline=f"Pool story {slug}" if headline is None else headline,
        seen_at=seen_at,
        markets=list(markets),
        source_key="f" * 64,
        first_collected_at=first_collected_at,
        last_collected_at=first_collected_at,
    )


async def test_collected_pool_reads_only_this_market_inside_the_24_hour_window(
    news_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = news_database
    now = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
    early = now - timedelta(hours=30)
    rows = {
        "window_start": _pool_row(
            "window-start", seen_at=now - timedelta(hours=24), first_collected_at=early
        ),
        "before_window": _pool_row(
            "before-window",
            seen_at=now - timedelta(hours=24, seconds=1),
            first_collected_at=now - timedelta(hours=1),
        ),
        "window_end": _pool_row("window-end", seen_at=now, first_collected_at=early),
        "after_window": _pool_row(
            "after-window", seen_at=now + timedelta(seconds=1), first_collected_at=early
        ),
        "undated_recent": _pool_row(
            "undated-recent", seen_at=None, first_collected_at=now - timedelta(hours=1)
        ),
        "undated_stale": _pool_row(
            "undated-stale", seen_at=None, first_collected_at=now - timedelta(hours=25)
        ),
        "shared_market": _pool_row(
            "shared-market",
            seen_at=now - timedelta(hours=2),
            first_collected_at=early,
            markets=("tw_equity", "global"),
        ),
        "other_market": _pool_row(
            "other-market",
            seen_at=now - timedelta(hours=2),
            first_collected_at=early,
            markets=("us_equity",),
        ),
        "blocked_host": _pool_row(
            "blocked-host",
            seen_at=now - timedelta(hours=2),
            first_collected_at=early,
            hostname="blocked.example",
        ),
        "invalid_headline": _pool_row(
            "invalid-headline",
            seen_at=now - timedelta(hours=3),
            first_collected_at=early,
            headline="",
        ),
    }
    async with sessions.begin() as database:
        database.add_all(rows.values())

    async with sessions() as database:
        pool = await load_collected_candidates(database, market="global", allowed=ALLOWED, now=now)

    assert [candidate.id for candidate in pool.candidates] == [
        rows[name].candidate_id
        for name in ("window_end", "undated_recent", "shared_market", "window_start")
    ]
    assert pool.invalid == 1
    undated = pool.candidates[1]
    assert undated.seen_at is None
    assert str(undated.url) == rows["undated_recent"].url
    assert undated.source_name == "Pool Wire"


class _Client:
    model_name = "test-news-model"
    selection_prompt_digest = "a" * 64
    selection_prompt_version = "selection-test"

    def __init__(self) -> None:
        self.selections = 0

    async def select(self, batch: list[FetchedCandidate], **_: object) -> ModelCall:
        del batch
        self.selections += 1
        return ModelCall(Selection(selections=()), None, None, None, 1, "b" * 64)

    async def aclose(self) -> None:
        return None


class _Pipeline:
    """Mock feed discovery and extraction; records what reached extraction."""

    def __init__(self, live: list[Candidate], live_bodies: dict[str, str]) -> None:
        self.live = live
        self.live_bodies = live_bodies
        self.extracted: list[list[str]] = []
        self.bodies: list[dict[str, str]] = []

    async def discover(self, *_: object, bodies: dict[str, str], **__: object) -> list[Candidate]:
        bodies.update(self.live_bodies)
        return list(self.live)

    async def extract(
        self, capped: list[Candidate], _: object, __: object, bodies: dict[str, str]
    ) -> list[ExtractionOutcome]:
        self.extracted.append([candidate.id for candidate in capped])
        self.bodies.append(dict(bodies))
        return [
            ExtractionOutcome(
                candidate,
                fetched=FetchedCandidate(
                    candidate,
                    str(candidate.url),
                    bodies.get(candidate.id, f"Article body for {candidate.headline}"),
                    hashlib.sha256(candidate.id.encode()).hexdigest(),
                ),
            )
            for candidate in capped
        ]


async def _add_refresh_job(sessions: async_sessionmaker[AsyncSession], now: datetime) -> None:
    async with sessions.begin() as database:
        job = JobRun(
            job_key="news_global_refresh_job",
            kind="function",
            trigger="manual",
            registry_version="test",
            registry_snapshot={},
            edition_date=now.date(),
            status="pending",
        )
        database.add(job)
        await database.flush()
        database.add(
            FunctionRun(
                job_run_id=job.id,
                function_key="news_global_refresh",
                provider_key="internal_services",
                scope={},
                status="pending",
            )
        )


async def _refresh(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    claim_at: datetime,
) -> tuple[uuid.UUID, FunctionOutcome, str]:
    claimed = await claim_ready_function(engine, sessions, owner="news-pool-test", now=claim_at)
    assert claimed is not None
    try:
        outcome = await orchestration_news_functions.refresh_news(settings, sessions, claimed)
    finally:
        # A pooled connection keeps session advisory locks, so release the
        # provider lock the way the worker does before handing it back.
        await claimed.connection.execute(text("SELECT pg_advisory_unlock_all()"))
        await claimed.connection.close()
    assert outcome.result is not None
    return claimed.function_run_id, outcome, str(outcome.result["batch_id"])


def _settings(*, collection_enabled: bool) -> Settings:
    return Settings(
        environment="test",
        daily_news_enabled=True,
        news_collection_enabled=collection_enabled,
        news_extra_hostnames="pool.example,live.example",
    )


async def _batch_rows(
    sessions: async_sessionmaker[AsyncSession], batch_id: str
) -> tuple[NewsCandidateBatch, dict[str, NewsCandidate]]:
    async with sessions() as database:
        batch = await database.get(NewsCandidateBatch, uuid.UUID(batch_id))
        assert batch is not None
        rows = {
            row.candidate_id: row
            for row in await database.scalars(
                select(NewsCandidate).where(NewsCandidate.batch_id == batch.id)
            )
        }
    return batch, rows


def _live_candidates() -> tuple[list[Candidate], dict[str, str]]:
    live = [
        Candidate(
            id=_candidate_id(f"https://live.example/{slug}"),
            url=f"https://live.example/{slug}",
            hostname="live.example",
            source_name="Live Wire",
            headline=headline,
            seen_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        for slug, headline in (
            ("shared", "Chipmakers rally after hours"),
            ("live-only", "Fed holds rates steady"),
        )
    ]
    return live, {live[0].id: "Full feed body " * 40}


async def test_refresh_merges_collected_pool_only_when_collection_is_enabled(
    news_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = news_database
    now = datetime.now(UTC)
    live, live_bodies = _live_candidates()
    shared_url = str(live[0].url)
    shared = _pool_row("shared", seen_at=now - timedelta(hours=10), first_collected_at=now)
    shared.url, shared.hostname = shared_url, "live.example"
    shared.candidate_id = live[0].id
    shared.headline = "Chipmakers rally (overnight headline)"
    collected = _pool_row(
        "overnight", seen_at=now - timedelta(hours=12), first_collected_at=now - timedelta(hours=12)
    )
    stale = _pool_row(
        "stale", seen_at=now - timedelta(hours=26), first_collected_at=now - timedelta(hours=12)
    )
    other_market = _pool_row(
        "us-only",
        seen_at=now - timedelta(hours=2),
        first_collected_at=now - timedelta(hours=2),
        markets=("us_equity",),
    )
    async with sessions.begin() as database:
        database.add_all([shared, collected, stale, other_market])

    pipeline = _Pipeline(live, live_bodies)
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(orchestration_news_functions, "_client", lambda _: _Client())
    monkeypatch.setattr(orchestration_news_functions, "discover_feed_candidates", pipeline.discover)
    monkeypatch.setattr(
        orchestration_news_functions, "_extract_candidate_outcomes", pipeline.extract
    )
    monkeypatch.setattr(
        orchestration_news_functions,
        "emit_event",
        lambda name, **fields: events.append((name, fields)),
    )
    await _add_refresh_job(sessions, now)
    _, _, disabled_batch_id = await _refresh(
        engine, sessions, _settings(collection_enabled=False), now
    )
    disabled_batch, disabled_rows = await _batch_rows(sessions, disabled_batch_id)

    assert set(pipeline.extracted[0]) == {candidate.id for candidate in live}
    assert {row.discovered_via for row in disabled_rows.values()} == {"live"}
    assert set(disabled_rows) == {candidate.id for candidate in live}
    assert disabled_batch.result is not None and disabled_batch.result["discovered"] == 2
    assert not [name for name, _ in events if name == "news.collection.merged"]

    await _add_refresh_job(sessions, now)
    _, enabled, enabled_batch_id = await _refresh(
        engine, sessions, _settings(collection_enabled=True), now
    )
    enabled_batch, enabled_rows = await _batch_rows(sessions, enabled_batch_id)

    assert set(pipeline.extracted[1]) == {live[0].id, live[1].id, collected.candidate_id}
    assert pipeline.bodies[1] == live_bodies
    assert {candidate_id: row.discovered_via for candidate_id, row in enabled_rows.items()} == {
        live[0].id: "both",
        live[1].id: "live",
        collected.candidate_id: "collected",
    }
    assert enabled_rows[live[0].id].headline == live[0].headline
    assert enabled_rows[collected.candidate_id].url == collected.url
    assert enabled_batch.result is not None and enabled_batch.result["discovered"] == 3
    assert [fields for name, fields in events if name == "news.collection.merged"] == [
        {
            "market": "global",
            "live": 1,
            "collected": 1,
            "both": 1,
            "duplicate_titles": 0,
            "invalid": 0,
        }
    ]
    assert enabled_batch.input_digest is not None
    assert enabled_batch.input_digest != disabled_batch.input_digest
    assert enabled.payload_digest == enabled_batch.input_digest


async def test_refresh_retry_reads_the_same_collected_pool(
    news_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = news_database
    now = datetime.now(UTC)
    live, live_bodies = _live_candidates()
    async with sessions.begin() as database:
        database.add_all(
            [
                _pool_row(
                    f"overnight-{index}",
                    seen_at=now - timedelta(hours=index + 1),
                    first_collected_at=now - timedelta(hours=index + 1),
                )
                for index in range(3)
            ]
        )
    pipeline = _Pipeline(live, live_bodies)
    client = _Client()
    monkeypatch.setattr(orchestration_news_functions, "_client", lambda _: client)
    monkeypatch.setattr(orchestration_news_functions, "discover_feed_candidates", pipeline.discover)
    monkeypatch.setattr(
        orchestration_news_functions, "_extract_candidate_outcomes", pipeline.extract
    )
    settings = _settings(collection_enabled=True)

    await _add_refresh_job(sessions, now)
    first_run, first, first_batch_id = await _refresh(engine, sessions, settings, now)
    first_selections = client.selections
    # An expired lease lets the same FunctionRun be claimed again, as after a
    # worker crash; the collector has stopped, so the pool is unchanged.
    retry_run, retry, retry_batch_id = await _refresh(
        engine, sessions, settings, now + LEASE_DURATION + timedelta(minutes=1)
    )

    assert retry_run == first_run
    assert retry_batch_id != first_batch_id
    assert pipeline.extracted[1] == pipeline.extracted[0]
    assert len(pipeline.extracted[0]) == 5
    assert retry.payload_digest == first.payload_digest
    # Identical selection input reuses every checkpointed model call.
    assert first_selections > 0
    assert client.selections == first_selections
