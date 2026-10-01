import hashlib
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
import pytest_asyncio
from conftest import remigrate_database
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import daily_insights_api.modules.orchestration.news_functions as orchestration_news_functions
from daily_insights_api.core.config import Settings
from daily_insights_api.modules.news.contracts import Candidate, Selection
from daily_insights_api.modules.news.editions import GLOBAL_SPEC
from daily_insights_api.modules.news.extraction import FetchedCandidate
from daily_insights_api.modules.news.failures import NewsOperationError
from daily_insights_api.modules.news.llm import (
    HeadlineScreen,
    ModelCall,
    ModelCallError,
    ScreenedHeadline,
)
from daily_insights_api.modules.news.models import (
    NewsCandidate,
    NewsCandidateBatch,
    NewsCollectedCandidate,
    NewsGenerationAudit,
)
from daily_insights_api.modules.news.screening import screen_input_digest
from daily_insights_api.modules.news.service import (
    ExtractionOutcome,
    _cap_discovery,
    _digest,
    _limit_candidates,
)
from daily_insights_api.modules.orchestration.models import FunctionAttempt, FunctionRun, JobRun
from daily_insights_api.modules.orchestration.worker import ClaimedFunction

pytestmark = pytest.mark.integration

EDITION = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    remigrate_database(database_url)
    engine: AsyncEngine = create_async_engine(database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _pool(size: int) -> list[Candidate]:
    return [
        Candidate(
            id=f"{index + 1:064x}",
            url=f"https://source{index % 10}.example/story-{index}",
            hostname=f"source{index % 10}.example",
            source_name=f"Source {index % 10}",
            headline=f"Story {index}",
            seen_at=NOW - timedelta(minutes=index),
        )
        for index in range(size)
    ]


class Client:
    """Stand-in model client: the screen answer is scripted, selection is empty."""

    model_name = "test-news-model"
    selection_prompt_digest = "a" * 64
    selection_prompt_version = "selection-test"
    screen_prompt_digest = "c" * 64
    screen_prompt_version = "screen-v1:cccccccccccc"

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.screen_calls: list[dict[str, Any]] = []
        self.selection_batches: list[list[str]] = []

    async def screen(
        self,
        headlines: Sequence[tuple[int, Candidate]],
        *,
        limit: int,
        shortlisted: tuple[str, ...] = (),
        retry_feedback: str | None = None,
        **_: object,
    ) -> ModelCall:
        self.screen_calls.append(
            {
                "numbers": [number for number, _ in headlines],
                "limit": limit,
                "shortlisted": shortlisted,
                "feedback": retry_feedback,
            }
        )
        value = self.answer(headlines, len(self.screen_calls), retry_feedback)
        return ModelCall(value, "screen-request", 100, 10, 5, f"{len(self.screen_calls):064x}")

    async def select(self, batch: list[FetchedCandidate], **_: object) -> ModelCall:
        self.selection_batches.append([item.candidate.id for item in batch])
        return ModelCall(Selection(selections=()), None, None, None, 1, "e" * 64)

    async def summarize(self, *_: object, **__: object) -> ModelCall:
        raise AssertionError("empty selections never reach summaries")

    async def translate(self, *_: object, **__: object) -> ModelCall:
        raise AssertionError("empty selections never reach translations")

    async def aclose(self) -> None:
        return None


class Harness:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        pool: list[Candidate],
        client: Client,
    ) -> None:
        self.sessions = sessions
        self.pool = pool
        self.client = client
        self.extracted: list[list[Candidate]] = []
        self.events: list[tuple[str, dict[str, object]]] = []
        self.function_run_id: uuid.UUID | None = None
        self.job_run_id: uuid.UUID | None = None
        self.attempts = 0

        async def discover(*_: object, **__: object) -> list[Candidate]:
            return list(self.pool)

        async def extract(candidates: list[Candidate], *_: object) -> list[ExtractionOutcome]:
            self.extracted.append(list(candidates))
            return [
                ExtractionOutcome(
                    candidate,
                    fetched=FetchedCandidate(
                        candidate,
                        str(candidate.url),
                        f"Body {candidate.headline}",
                        candidate.id,
                        candidate.seen_at,
                    ),
                )
                for candidate in candidates
            ]

        monkeypatch.setattr(orchestration_news_functions, "_client", lambda _: client)
        monkeypatch.setattr(orchestration_news_functions, "discover_feed_candidates", discover)
        monkeypatch.setattr(orchestration_news_functions, "_extract_candidate_outcomes", extract)
        monkeypatch.setattr(
            orchestration_news_functions,
            "emit_event",
            lambda name, **fields: self.events.append((name, fields)),
        )

    async def claim(self) -> ClaimedFunction:
        """Start the function run, or a resumed attempt of the same run."""
        fence = uuid.uuid4()
        self.attempts += 1
        async with self.sessions.begin() as database:
            if self.function_run_id is None:
                job = JobRun(
                    job_key="news_global_refresh_job",
                    kind="function",
                    trigger="manual",
                    registry_version="test",
                    registry_snapshot={},
                    edition_date=EDITION,
                    status="running",
                )
                database.add(job)
                await database.flush()
                run = FunctionRun(
                    job_run_id=job.id,
                    function_key="news_global_refresh",
                    provider_key="internal_services",
                    scope={},
                    status="running",
                    lease_token=fence,
                )
                database.add(run)
                await database.flush()
                self.function_run_id = run.id
                self.job_run_id = job.id
            else:
                stored = await database.get(FunctionRun, self.function_run_id)
                assert stored is not None
                stored.lease_token = fence
            attempt = FunctionAttempt(
                function_run_id=self.function_run_id,
                attempt_number=self.attempts,
                provider_key="internal_services",
                function_key="news_global_refresh",
                scope={},
                fence_token=fence,
                status="running",
                request_metadata=[],
            )
            database.add(attempt)
            await database.flush()
            attempt_id = attempt.id
        assert self.job_run_id is not None
        return ClaimedFunction(
            connection=cast(Any, None),
            function_run_id=self.function_run_id,
            job_run_id=self.job_run_id,
            attempt_id=attempt_id,
            function_key="news_global_refresh",
            provider_key="internal_services",
            edition_date=EDITION,
            fence_token=fence,
            deadline_at=None,
            scope={},
        )

    async def refresh(
        self, *, screen_enabled: bool = True, collection_enabled: bool = False
    ) -> uuid.UUID:
        claimed = await self.claim()
        outcome = await orchestration_news_functions.refresh_news(
            Settings(
                environment="test",
                daily_news_enabled=True,
                news_collection_enabled=collection_enabled,
                news_headline_screen_enabled=screen_enabled,
            ),
            self.sessions,
            claimed,
        )
        assert outcome.result is not None
        return uuid.UUID(cast(str, outcome.result["batch_id"]))

    async def rows(self, batch_id: uuid.UUID) -> dict[str, NewsCandidate]:
        async with self.sessions() as database:
            return {
                row.candidate_id: row
                for row in await database.scalars(
                    select(NewsCandidate).where(NewsCandidate.batch_id == batch_id)
                )
            }

    async def batch(self, batch_id: uuid.UUID) -> NewsCandidateBatch:
        async with self.sessions() as database:
            stored = await database.get(NewsCandidateBatch, batch_id)
            assert stored is not None
            return stored

    async def screen_audits(self) -> list[NewsGenerationAudit]:
        async with self.sessions() as database:
            return list(
                await database.scalars(
                    select(NewsGenerationAudit)
                    .where(NewsGenerationAudit.stage == "screen")
                    .order_by(NewsGenerationAudit.created_at)
                )
            )

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events if name.startswith("news.screen.")]


def _screen_answer(scores: dict[int, int]) -> Any:
    def answer(
        headlines: Sequence[tuple[int, Candidate]], call: int, feedback: str | None
    ) -> HeadlineScreen:
        del call, feedback
        numbers = {number for number, _ in headlines}
        return HeadlineScreen(
            shortlist=tuple(
                ScreenedHeadline(n=number, score=score)
                for number, score in scores.items()
                if number in numbers
            )
        )

    return answer


def _current_extraction(pool: list[Candidate]) -> list[Candidate]:
    return _cap_discovery(
        pool,
        per_source=GLOBAL_SPEC.max_discovery_per_source,
        total=GLOBAL_SPEC.max_discovery_total,
        full_text_ids=frozenset(),
        interleave=GLOBAL_SPEC.interleave_sources,
        impact_patterns=GLOBAL_SPEC.headline_impact_patterns,
    )


def _current_digest(pool: list[Candidate]) -> str:
    extracted = [
        FetchedCandidate(
            candidate,
            str(candidate.url),
            f"Body {candidate.headline}",
            candidate.id,
            candidate.seen_at,
        )
        for candidate in _current_extraction(pool)
    ]
    usable = _limit_candidates(
        extracted,
        total=GLOBAL_SPEC.max_candidates * 2,
        per_source=GLOBAL_SPEC.max_per_source,
        interleave=GLOBAL_SPEC.interleave_sources,
        impact_patterns=GLOBAL_SPEC.headline_impact_patterns,
    )
    return _digest(usable, "test-news-model", "a" * 64, "global", GLOBAL_SPEC.selection)


async def test_screen_shortlists_pool_in_two_calls_and_records_ranks(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _pool(250)
    # First call keeps 40 headlines at 3; the second call adds 30 at 4, so
    # the merged shortlist leads with the second call and is cut at 60.
    scores = {number: 3 for number in range(1, 41)} | {number: 4 for number in range(201, 231)}
    harness = Harness(sessions, monkeypatch, pool, Client(_screen_answer(scores)))

    batch_id = await harness.refresh()

    calls = harness.client.screen_calls
    assert [call["numbers"] for call in calls] == [
        list(range(1, 201)),
        list(range(201, 251)),
    ]
    assert [call["limit"] for call in calls] == [60, 60]
    assert calls[0]["shortlisted"] == ()
    assert calls[1]["shortlisted"] == tuple(f"Story {number - 1}" for number in range(1, 41))
    expected = [pool[number - 1] for number in range(201, 231)] + [
        pool[number - 1] for number in range(1, 31)
    ]
    assert harness.extracted == [expected]

    rows = await harness.rows(batch_id)
    for rank, candidate in enumerate(expected, start=1):
        assert (rows[candidate.id].screen_rank, rows[candidate.id].screen_score) == (
            rank,
            4 if rank <= 30 else 3,
        )
        assert rows[candidate.id].stage != "screened_out"
    screened_out = {
        candidate_id for candidate_id, row in rows.items() if row.stage == "screened_out"
    }
    assert screened_out == {candidate.id for candidate in pool} - {
        candidate.id for candidate in expected
    }
    assert all(rows[candidate_id].screen_rank is None for candidate_id in screened_out)

    # Extracted articles keep the screen order, the per-source cap and the 40 total.
    selected = harness.client.selection_batches[0] + harness.client.selection_batches[1]
    assert len(selected) == 40
    assert max(Counter(rows[candidate_id].hostname for candidate_id in selected).values()) <= 5
    ranks = [rows[candidate_id].screen_rank for candidate_id in selected]
    assert ranks == sorted(cast(list[int], ranks))

    audits = await harness.screen_audits()
    assert [(audit.status, audit.prompt_version) for audit in audits] == [
        ("succeeded", "screen-v1:cccccccccccc"),
        ("succeeded", "screen-v1:cccccccccccc"),
    ]
    batch = await harness.batch(batch_id)
    assert batch.result is not None
    assert batch.result["screen"] == {
        "status": "shortlisted",
        "pool": 250,
        "truncated": 0,
        "calls": 2,
        "screened": 250,
        "shortlisted": 60,
        "fallback_code": None,
    }
    assert batch.result["prompt_version"].startswith("screen-v1:cccccccccccc+selection-test+")
    assert batch.input_digest != _current_digest(pool)
    assert harness.event_names() == []


async def test_screen_truncates_pool_over_400_with_an_event(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _pool(450)
    harness = Harness(sessions, monkeypatch, pool, Client(_screen_answer({1: 5, 2: 4, 201: 3})))

    batch_id = await harness.refresh()

    assert [len(call["numbers"]) for call in harness.client.screen_calls] == [200, 200]
    assert ("news.screen.truncated", {"market": "global", "pool": 450, "truncated": 50}) in (
        harness.events
    )
    rows = await harness.rows(batch_id)
    # The oldest 50 never reached the model, so they are not "screened out".
    assert {rows[candidate.id].stage for candidate in pool[400:]} == {"discovered"}
    assert sum(row.stage == "screened_out" for row in rows.values()) == 397
    batch = await harness.batch(batch_id)
    assert batch.result is not None and batch.result["screen"]["truncated"] == 50


async def test_screen_repairs_once_then_uses_the_repaired_answer(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _pool(5)

    def answer(
        headlines: Sequence[tuple[int, Candidate]], call: int, feedback: str | None
    ) -> HeadlineScreen:
        del headlines, feedback
        if call == 1:
            raise ModelCallError(
                "headline screen output failed schema validation",
                input_digest="1" * 64,
                latency_ms=4,
                error_code="screen_schema_invalid",
                validation_issues=("shortlist.0.score:less_than_equal",),
            )
        return HeadlineScreen(shortlist=(ScreenedHeadline(n=2, score=5),))

    harness = Harness(sessions, monkeypatch, pool, Client(answer))

    batch_id = await harness.refresh()

    assert [call["feedback"] for call in harness.client.screen_calls] == [
        None,
        "screen_schema_invalid",
    ]
    assert harness.extracted == [[pool[1]]]
    audits = await harness.screen_audits()
    assert [(audit.status, audit.error_code) for audit in audits] == [
        ("failed", "screen_schema_invalid"),
        ("succeeded", None),
    ]
    rows = await harness.rows(batch_id)
    assert (rows[pool[1].id].screen_rank, rows[pool[1].id].screen_score) == (1, 5)
    assert harness.event_names() == []


async def test_screen_falls_back_after_repair_and_resume_does_not_renew_budget(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _pool(30)

    def answer(
        headlines: Sequence[tuple[int, Candidate]], call: int, feedback: str | None
    ) -> HeadlineScreen:
        del headlines, call, feedback
        raise ModelCallError(
            "headline screen output failed schema validation",
            input_digest="1" * 64,
            latency_ms=4,
            error_code="screen_schema_invalid",
        )

    harness = Harness(sessions, monkeypatch, pool, Client(answer))

    batch_id = await harness.refresh()

    assert len(harness.client.screen_calls) == 2
    assert harness.extracted == [_current_extraction(pool)]
    fallback_events = [fields for name, fields in harness.events if name == "news.screen.fallback"]
    assert fallback_events == [
        {
            "market": "global",
            "call": 1,
            "error_code": "screen_schema_invalid_exhausted",
            "validation_issues": [],
        }
    ]
    rows = await harness.rows(batch_id)
    assert all(row.stage != "screened_out" for row in rows.values())
    assert all(row.screen_rank is None and row.screen_score is None for row in rows.values())
    batch = await harness.batch(batch_id)
    assert batch.result is not None
    assert batch.result["screen"]["status"] == "fallback"
    # The flag stays in the digest even when the model answer is unusable.
    assert batch.input_digest == screen_input_digest(
        _current_digest(pool), "c" * 64, "screen-v1:cccccccccccc"
    )

    resumed_batch_id = await harness.refresh()

    assert len(harness.client.screen_calls) == 2
    assert harness.extracted[-1] == _current_extraction(pool)
    assert len(await harness.screen_audits()) == 2
    resumed = await harness.batch(resumed_batch_id)
    assert resumed.result is not None and resumed.result["screen"]["status"] == "fallback"


async def test_screen_success_is_reused_when_the_function_resumes(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _pool(220)
    harness = Harness(
        sessions, monkeypatch, pool, Client(_screen_answer({3: 5, 1: 4, 210: 4, 2: 2}))
    )

    first_batch_id = await harness.refresh()
    resumed_batch_id = await harness.refresh()

    assert len(harness.client.screen_calls) == 2
    assert harness.extracted[0] == harness.extracted[1]
    assert len(await harness.screen_audits()) == 2
    first_rows = await harness.rows(first_batch_id)
    resumed_rows = await harness.rows(resumed_batch_id)
    assert {
        candidate_id: (row.stage, row.screen_rank, row.screen_score)
        for candidate_id, row in first_rows.items()
    } == {
        candidate_id: (row.stage, row.screen_rank, row.screen_score)
        for candidate_id, row in resumed_rows.items()
    }
    assert [pool[number - 1].id for number in (3, 1, 210, 2)] == [
        candidate.id for candidate in harness.extracted[0]
    ]


async def test_screen_systemic_provider_failure_fails_the_refresh(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer(
        headlines: Sequence[tuple[int, Candidate]], call: int, feedback: str | None
    ) -> HeadlineScreen:
        del headlines, call, feedback
        raise ModelCallError(
            "model completion request failed",
            input_digest="1" * 64,
            latency_ms=4,
            error_code="provider_http_429",
        )

    harness = Harness(sessions, monkeypatch, _pool(10), Client(answer))

    with pytest.raises(NewsOperationError) as caught:
        await harness.refresh()

    assert (caught.value.failure.code, caught.value.failure.action) == (
        "provider_http_429",
        "retry",
    )
    assert len(harness.client.screen_calls) == 1
    assert harness.extracted == []
    async with sessions() as database:
        batches = list(await database.scalars(select(NewsCandidateBatch)))
    assert [batch.status for batch in batches] == ["failed"]


async def test_disabled_screen_keeps_the_current_pipeline(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _pool(250)

    def answer(*_: object) -> HeadlineScreen:
        raise AssertionError("screening is disabled")

    harness = Harness(sessions, monkeypatch, pool, Client(answer))

    batch_id = await harness.refresh(screen_enabled=False)

    assert harness.client.screen_calls == []
    assert harness.extracted == [_current_extraction(pool)]
    batch = await harness.batch(batch_id)
    assert batch.input_digest == _current_digest(pool)
    assert batch.result is not None
    assert "screen" not in batch.result
    assert batch.result["prompt_version"].startswith("selection-test+")
    rows = await harness.rows(batch_id)
    assert all(row.screen_rank is None and row.stage != "screened_out" for row in rows.values())
    assert await harness.screen_audits() == []


async def test_screen_ranks_collected_pool_candidates_beside_live_discovery(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With both flags on, stories only the overnight pool kept reach the screen."""
    live = _pool(3)
    collected_at = datetime.now(UTC) - timedelta(hours=10)
    rows_to_collect = [
        # A story live discovery also returned: the live candidate is kept.
        (live[0].id, str(live[0].url), live[0].headline),
        # A story that scrolled off its feed before the refresh.
        (
            hashlib.sha256(b"https://www.cnbc.com/2026/09/29/overnight-story.html").hexdigest(),
            "https://www.cnbc.com/2026/09/29/overnight-story.html",
            "Overnight central bank decision",
        ),
    ]
    async with sessions.begin() as database:
        database.add_all(
            NewsCollectedCandidate(
                candidate_id=candidate_id,
                url=url,
                hostname="www.cnbc.com",
                source_name="CNBC",
                headline=headline,
                seen_at=collected_at,
                markets=["global"],
                source_key="f" * 64,
                first_collected_at=collected_at,
                last_collected_at=collected_at,
            )
            for candidate_id, url, headline in rows_to_collect
        )

    def answer(
        headlines: Sequence[tuple[int, Candidate]], call: int, feedback: str | None
    ) -> HeadlineScreen:
        del call, feedback
        return HeadlineScreen(
            shortlist=tuple(ScreenedHeadline(n=number, score=3) for number, _ in headlines)
        )

    harness = Harness(sessions, monkeypatch, live, Client(answer))

    batch_id = await harness.refresh(collection_enabled=True)

    overnight_id = rows_to_collect[1][0]
    assert [len(call["numbers"]) for call in harness.client.screen_calls] == [4]
    assert overnight_id in {candidate.id for candidate in harness.extracted[0]}
    rows = await harness.rows(batch_id)
    assert {candidate_id: row.discovered_via for candidate_id, row in rows.items()} == {
        live[0].id: "both",
        live[1].id: "live",
        live[2].id: "live",
        overnight_id: "collected",
    }
    assert all(row.screen_rank is not None for row in rows.values())
    assert rows[live[0].id].hostname == live[0].hostname
    merged = [fields for name, fields in harness.events if name == "news.collection.merged"]
    assert merged == [
        {
            "market": "global",
            "live": 2,
            "collected": 1,
            "both": 1,
            "duplicate_titles": 0,
            "invalid": 0,
        }
    ]
