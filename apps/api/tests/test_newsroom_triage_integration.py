import asyncio
import math
import os
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings
from daily_insights_api.core.models import Base
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.newsroom import clock, events_service, triage
from daily_insights_api.modules.newsroom.contracts import EventNew
from daily_insights_api.modules.newsroom.events_service import EventServiceError
from daily_insights_api.modules.newsroom.models import (
    EMBEDDING_DIMENSIONS,
    NewsroomArticle,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEditLog,
    NewsroomEvent,
    NewsroomLlmCall,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.notifier import LogNotifier
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.queue import claim, run_claimed
from daily_insights_api.modules.newsroom.worker import Runtime, StageBinding

pytestmark = pytest.mark.integration

WINDOW = date(2026, 10, 1)
ResultT = TypeVar("ResultT", bound=BaseModel)
Reply = Callable[[dict[str, Any]], Any]


def vector(degrees: float) -> list[float]:
    """Unit vector in the first plane: cosine similarity is cos(angle difference)."""
    radians = math.radians(degrees)
    return [math.cos(radians), math.sin(radians)] + [0.0] * (EMBEDDING_DIMENSIONS - 2)


@dataclass
class FakeEmbedder:
    angles: dict[str, float]
    texts: list[str] = field(default_factory=list)

    async def embed(
        self, database: AsyncSession, texts: Sequence[str], *, audit: CallAudit
    ) -> list[list[float]]:
        self.texts.extend(texts)
        database.add(
            NewsroomLlmCall(stage=audit.stage, subject_id=audit.subject_id, model="e", latency_ms=1)
        )
        return [vector(self.angles[text.split("\n")[0]]) for text in texts]


@dataclass
class FakeJsonModel:
    reply: Reply
    payloads: list[dict[str, Any]] = field(default_factory=list)
    barrier: asyncio.Barrier | None = None

    async def complete(
        self,
        database: AsyncSession,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        result_type: type[ResultT],
        audit: CallAudit,
    ) -> ResultT:
        assert system == triage.load_triage_prompt().text
        self.payloads.append(payload)
        if self.barrier is not None:
            await self.barrier.wait()
        database.add(
            NewsroomLlmCall(
                stage=audit.stage,
                subject_id=audit.subject_id,
                model=model,
                prompt_version=audit.prompt_version,
                latency_ms=1,
            )
        )
        return result_type.model_validate(self.reply(payload))


def relevant(event: dict[str, str]) -> dict[str, Any]:
    return {
        "relevant": True,
        "topic": "policy",
        "market_scores": {"global": 90, "tw_equity": 40, "us_equity": 75},
        "event": event,
    }


@pytest_asyncio.fixture
async def newsroom_database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required for integration tests")
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


def _runtime(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    llm: FakeJsonModel | None = None,
    embedder: FakeEmbedder | None = None,
) -> Runtime:
    return Runtime(
        settings=Settings(newsroom_triage_model="triage-test-model"),
        session_factory=session_factory,
        llm=llm or FakeJsonModel(lambda payload: relevant({"new": "unused"})),
        embedder=embedder or FakeEmbedder({}),
        notifier=LogNotifier(),
    )


def _binding(runtime: Runtime, stage: str) -> StageBinding:
    return next(b for b in triage.register(runtime).stages if b.stage.name == stage)


async def _run(runtime: Runtime, stage: str) -> list[str]:
    binding = _binding(runtime, stage)
    async with runtime.session_factory() as database:
        claims = await claim(database, binding.stage, limit=20, extra_filter=binding.extra_filter)
    return [await run_claimed(runtime.session_factory, item, binding.handler) for item in claims]


async def _source(
    session_factory: async_sessionmaker[AsyncSession], key: str = "wire", *, trust_tier: int = 2
) -> uuid.UUID:
    async with session_factory() as database:
        source = NewsroomSource(
            key=key,
            name=key.title(),
            kind="rss",
            hostname=f"{key}.example.com",
            markets=["global"],
            trust_tier=trust_tier,
        )
        database.add(source)
        await database.commit()
        return source.id


async def _event(
    session_factory: async_sessionmaker[AsyncSession],
    title: str,
    *,
    edition_date: date = WINDOW,
    analysis_status: str = "idle",
) -> uuid.UUID:
    async with session_factory() as database:
        event = NewsroomEvent(
            edition_date=edition_date, working_title=title, analysis_status=analysis_status
        )
        database.add(event)
        await database.commit()
        return event.id


async def _article(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
    title: str,
    *,
    angle: float | None = None,
    event_id: uuid.UUID | None = None,
    edition_date: date = WINDOW,
    seen_minutes: int = 0,
    **columns: Any,
) -> uuid.UUID:
    async with session_factory() as database:
        article = NewsroomArticle(
            source_id=source_id,
            url=f"https://example.com/{uuid.uuid4()}",
            url_hash=uuid.uuid4().hex * 2,
            title=title,
            edition_date=edition_date,
            first_seen_at=datetime(2026, 9, 30, 1, tzinfo=UTC) + timedelta(minutes=seen_minutes),
            embedding=None if angle is None else vector(angle),
            event_id=event_id,
            **columns,
        )
        database.add(article)
        await database.commit()
        return article.id


async def _queued_for_triage(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
    title: str,
    angle: float,
    **columns: Any,
) -> uuid.UUID:
    columns.setdefault("fetch_status", "done")
    return await _article(
        session_factory,
        source_id,
        title,
        angle=angle,
        embed_status="done",
        triage_status="pending",
        **columns,
    )


async def _get(
    session_factory: async_sessionmaker[AsyncSession], model: Any, row_id: uuid.UUID
) -> Any:
    async with session_factory() as database:
        row = await database.get(model, row_id)
        assert row is not None
        return row


async def _count(session_factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with session_factory() as database:
        return int(await database.scalar(select(func.count()).select_from(model)) or 0)


# ---------------------------------------------------------------- embed stage


async def test_embed_writes_vector_and_hands_off_to_triage(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _article(
        newsroom_database,
        source_id,
        "Fed holds rates",
        feed_summary="  The Fed kept rates\nunchanged. ",
        embed_status="pending",
    )
    embedder = FakeEmbedder({"Fed holds rates": 10})
    runtime = _runtime(newsroom_database, embedder=embedder)

    assert await _run(runtime, "embed") == ["done"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert embedder.texts == ["Fed holds rates\nThe Fed kept rates unchanged."]
    assert row.embed_status == "done"
    assert list(row.embedding) == pytest.approx(vector(10))
    assert (row.triage_status, row.triage_attempts, row.triage_next_attempt_at) == (
        "pending",
        0,
        None,
    )
    async with newsroom_database() as database:
        audit = (await database.scalars(select(NewsroomLlmCall))).one()
    assert (audit.stage, audit.subject_id) == ("embed", article_id)


async def test_embed_with_empty_input_fails_without_a_call(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _article(newsroom_database, source_id, "   ", embed_status="pending")
    embedder = FakeEmbedder({})

    assert await _run(_runtime(newsroom_database, embedder=embedder), "embed") == ["done"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert (row.embed_status, row.embed_error_code) == ("failed", "embed_input_empty")
    assert row.triage_status == "idle"
    assert embedder.texts == []


# --------------------------------------------------------------- triage stage


async def test_triage_waits_until_the_fetch_has_a_verdict(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _queued_for_triage(
        newsroom_database, source_id, "Fed holds rates", 0, fetch_status="pending"
    )
    runtime = _runtime(newsroom_database)
    assert await _run(runtime, "triage") == []

    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomArticle)
            .where(NewsroomArticle.id == article_id)
            .values(fetch_status="failed", body_status="unavailable")
        )
        await database.commit()
    assert await _run(runtime, "triage") == ["done"]


async def test_first_article_opens_a_new_event(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database, trust_tier=3)
    article_id = await _queued_for_triage(
        newsroom_database,
        source_id,
        "Fed holds rates",
        0,
        feed_summary="Summary",
        body="b" * 3_000,
        body_status="ok",
    )
    llm = FakeJsonModel(lambda payload: relevant({"new": "聯準會維持利率不變"}))

    assert await _run(_runtime(newsroom_database, llm=llm), "triage") == ["done"]

    [payload] = llm.payloads
    assert payload["candidate_events"] == []
    assert payload["article"]["source"] == {"name": "Wire", "trust_tier": 3}
    assert payload["article"]["body_excerpt"] == "b" * triage.BODY_EXCERPT_CHARS
    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert row.triage_status == "done"
    assert (row.relevant, row.topic) == (True, "policy")
    assert row.market_scores == {"global": 90, "tw_equity": 40, "us_equity": 75}
    assert row.triaged_at is not None
    event = await _get(newsroom_database, NewsroomEvent, row.event_id)
    assert (event.working_title, event.created_by, event.status, event.edition_date) == (
        "聯準會維持利率不變",
        "triage",
        "open",
        WINDOW,
    )
    async with newsroom_database() as database:
        audit = (await database.scalars(select(NewsroomLlmCall))).one()
    assert (audit.stage, audit.subject_id, audit.model) == (
        "triage",
        article_id,
        "triage-test-model",
    )
    assert audit.prompt_version == triage.load_triage_prompt().version
    assert audit.error_code is None


async def test_body_is_withheld_unless_the_fetch_succeeded(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    await _queued_for_triage(
        newsroom_database,
        source_id,
        "Fed holds rates",
        0,
        body="short nav text",
        body_status="rejected",
    )
    llm = FakeJsonModel(lambda payload: relevant({"new": "聯準會維持利率不變"}))
    await _run(_runtime(newsroom_database, llm=llm), "triage")
    assert "body_excerpt" not in llm.payloads[0]["article"]


async def test_article_joins_the_matched_window_event(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    trusted = await _source(newsroom_database, "trusted", trust_tier=3)
    minor = await _source(newsroom_database, "minor", trust_tier=1)
    fed = await _event(newsroom_database, "Fed decision")
    await _article(newsroom_database, minor, "Minor: Fed", angle=2, event_id=fed, seen_minutes=0)
    await _article(
        newsroom_database, trusted, "Trusted: Fed", angle=4, event_id=fed, seen_minutes=5
    )
    await _article(newsroom_database, minor, "Late minor", angle=0, event_id=fed, seen_minutes=9)
    # Six more open events in the window: only the nearest five are offered.
    others = [await _event(newsroom_database, f"Other {n}") for n in range(6)]
    for n, other in enumerate(others):
        await _article(newsroom_database, minor, f"Other {n}", angle=20 + 10 * n, event_id=other)
    # Neither another window's event nor a merged event is a candidate.
    yesterday = await _event(newsroom_database, "Yesterday", edition_date=WINDOW - timedelta(1))
    await _article(
        newsroom_database,
        minor,
        "Yesterday",
        angle=1,
        event_id=yesterday,
        edition_date=WINDOW - timedelta(1),
    )
    merged = await _event(newsroom_database, "Merged")
    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomEvent)
            .where(NewsroomEvent.id == merged)
            .values(status="merged", merged_into_id=fed)
        )
        await database.commit()
    await _article(newsroom_database, minor, "Merged", angle=1, event_id=merged)

    article_id = await _queued_for_triage(newsroom_database, minor, "Fed holds rates", 1)
    llm = FakeJsonModel(lambda payload: relevant({"match": payload["candidate_events"][0]["id"]}))

    assert await _run(_runtime(newsroom_database, llm=llm), "triage") == ["done"]

    candidates = llm.payloads[0]["candidate_events"]
    assert [c["id"] for c in candidates] == [str(fed), *(str(o) for o in others[:4])]
    assert candidates[0] == {
        "id": str(fed),
        "working_title": "Fed decision",
        "titles": ["Trusted: Fed", "Minor: Fed"],
    }
    assert candidates[1]["titles"] == ["Other 0"]
    assert (await _get(newsroom_database, NewsroomArticle, article_id)).event_id == fed
    assert await _count(newsroom_database, NewsroomEvent) == 9


async def test_irrelevant_article_is_scored_but_not_clustered(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _queued_for_triage(newsroom_database, source_id, "Celebrity wedding", 0)
    llm = FakeJsonModel(
        lambda payload: {
            "relevant": False,
            "topic": "companies",
            "market_scores": {"global": 0, "tw_equity": 3, "us_equity": 0},
            "event": {"new": "should be ignored"},
        }
    )

    assert await _run(_runtime(newsroom_database, llm=llm), "triage") == ["done"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert (row.triage_status, row.relevant, row.event_id) == ("done", False, None)
    assert row.market_scores == {"global": 0, "tw_equity": 3, "us_equity": 0}
    assert await _count(newsroom_database, NewsroomEvent) == 0


async def test_match_outside_the_candidates_is_retried_and_audited(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    stranger = await _event(newsroom_database, "Not offered", edition_date=WINDOW + timedelta(1))
    article_id = await _queued_for_triage(newsroom_database, source_id, "Fed holds rates", 0)
    llm = FakeJsonModel(lambda payload: relevant({"match": str(stranger)}))

    assert await _run(_runtime(newsroom_database, llm=llm), "triage") == ["pending"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert (row.triage_status, row.triage_error_code) == ("pending", "triage_schema_invalid")
    assert (row.relevant, row.event_id) == (None, None)
    assert row.triage_next_attempt_at is not None
    async with newsroom_database() as database:
        audit = (await database.scalars(select(NewsroomLlmCall))).one()
    assert (audit.subject_id, audit.error_code) == (article_id, "triage_schema_invalid")


async def test_concurrent_articles_of_one_story_open_a_single_event(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    first = await _queued_for_triage(newsroom_database, source_id, "Fed holds rates", 0)
    second = await _queued_for_triage(newsroom_database, source_id, "Fed keeps rates", 5)
    # Both model calls are in flight together, so both see no candidate and say "new".
    llm = FakeJsonModel(
        lambda payload: relevant({"new": payload["article"]["title"]}), barrier=asyncio.Barrier(2)
    )
    runtime = _runtime(newsroom_database, llm=llm)
    binding = _binding(runtime, "triage")
    async with newsroom_database() as database:
        claims = await claim(database, binding.stage, limit=2, extra_filter=binding.extra_filter)

    outcomes = await asyncio.gather(
        *(run_claimed(newsroom_database, item, binding.handler) for item in claims)
    )

    assert outcomes == ["done", "done"]
    assert all(payload["candidate_events"] == [] for payload in llm.payloads)
    assert await _count(newsroom_database, NewsroomEvent) == 1
    rows = [await _get(newsroom_database, NewsroomArticle, a) for a in (first, second)]
    assert rows[0].event_id is not None and rows[0].event_id == rows[1].event_id


async def test_concurrent_unrelated_articles_open_separate_events(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    await _queued_for_triage(newsroom_database, source_id, "Fed holds rates", 0)
    # cos(30°) ≈ 0.866 sits just under the same-event threshold.
    await _queued_for_triage(newsroom_database, source_id, "Oil supply cut", 30)
    llm = FakeJsonModel(
        lambda payload: relevant({"new": payload["article"]["title"]}), barrier=asyncio.Barrier(2)
    )
    runtime = _runtime(newsroom_database, llm=llm)
    binding = _binding(runtime, "triage")
    async with newsroom_database() as database:
        claims = await claim(database, binding.stage, limit=2, extra_filter=binding.extra_filter)

    await asyncio.gather(*(run_claimed(newsroom_database, c, binding.handler) for c in claims))

    async with newsroom_database() as database:
        titles = sorted((await database.scalars(select(NewsroomEvent.working_title))).all())
    assert titles == ["Fed holds rates", "Oil supply cut"]


async def test_match_on_an_event_merged_meanwhile_lands_on_the_target(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    target = await _event(newsroom_database, "Target")
    absorbed = await _event(newsroom_database, "Absorbed")
    article_id = await _article(newsroom_database, source_id, "Fed", angle=0)
    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomEvent)
            .where(NewsroomEvent.id == absorbed)
            .values(status="merged", merged_into_id=target)
        )
        resolved = await triage.assign_event(
            database,
            article_id=article_id,
            edition_date=WINDOW,
            embedding=vector(0),
            choice=absorbed,
        )
        await database.rollback()
    assert resolved == target


async def test_new_event_choice_reuses_a_near_identical_event_under_the_lock(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    existing = await _event(newsroom_database, "Fed decision")
    await _article(newsroom_database, source_id, "Fed", angle=0, event_id=existing)
    article_id = await _article(newsroom_database, source_id, "Fed again", angle=20)
    async with newsroom_database() as database:
        # cos(20°) ≈ 0.94 is above the threshold, so "new" joins the existing event.
        joined = await triage.assign_event(
            database,
            article_id=article_id,
            edition_date=WINDOW,
            embedding=vector(20),
            choice=EventNew(new="Fed again"),
        )
        far = await triage.assign_event(
            database,
            article_id=article_id,
            edition_date=WINDOW,
            embedding=vector(40),
            choice=EventNew(new="Something else"),
        )
        await database.rollback()
    assert joined == existing
    assert far != existing


# ------------------------------------------------------------ events service


async def _user(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as database:
        user = User(
            email=f"editor-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Editor",
            password_hash="x",
        )
        database.add(user)
        await database.commit()
        return user.id


async def _edition(session_factory: async_sessionmaker[AsyncSession], market: str) -> uuid.UUID:
    async with session_factory() as database:
        edition = NewsroomEdition(
            edition_date=WINDOW,
            market_code=market,
            auto_publish_at=clock.auto_publish_at(WINDOW),
            late_fill_deadline=clock.late_fill_deadline(WINDOW),
        )
        database.add(edition)
        await database.commit()
        return edition.id


async def _item(
    session_factory: async_sessionmaker[AsyncSession],
    edition_id: uuid.UUID,
    event_id: uuid.UUID,
    rank: int,
    **columns: Any,
) -> uuid.UUID:
    async with session_factory() as database:
        item = NewsroomEditionItem(edition_id=edition_id, event_id=event_id, rank=rank, **columns)
        database.add(item)
        await database.commit()
        return item.id


async def _edits(
    session_factory: async_sessionmaker[AsyncSession], entity_id: uuid.UUID
) -> list[NewsroomEditLog]:
    async with session_factory() as database:
        return list(
            (
                await database.scalars(
                    select(NewsroomEditLog).where(NewsroomEditLog.entity_id == entity_id)
                )
            ).all()
        )


def _logged(value: dict[str, Any] | None) -> dict[str, Any]:
    assert value is not None
    return value


async def test_merge_moves_articles_and_items_and_requeues_the_target(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _user(newsroom_database)
    source_id = await _source(newsroom_database)
    target = await _event(newsroom_database, "Target", analysis_status="ready")
    first = await _event(newsroom_database, "First", analysis_status="ready")
    second = await _event(newsroom_database, "Second")
    older = await _event(newsroom_database, "Merged earlier")
    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomEvent)
            .where(NewsroomEvent.id == older)
            .values(status="merged", merged_into_id=first)
        )
        await database.execute(
            update(NewsroomEvent).where(NewsroomEvent.id == target).values(analysis_attempts=3)
        )
        await database.commit()
    kept = await _article(newsroom_database, source_id, "t", angle=0, event_id=target)
    moved = [
        await _article(newsroom_database, source_id, "a", angle=0, event_id=first),
        await _article(newsroom_database, source_id, "b", angle=0, event_id=first),
        await _article(newsroom_database, source_id, "c", angle=0, event_id=second),
    ]
    global_ = await _edition(newsroom_database, "global")
    tw = await _edition(newsroom_database, "tw_equity")
    us = await _edition(newsroom_database, "us_equity")
    await _item(newsroom_database, global_, target, 1)
    dropped = await _item(newsroom_database, global_, first, 2)
    repointed = await _item(newsroom_database, tw, first, 1)
    us_first = await _item(newsroom_database, us, first, 1)
    us_second = await _item(newsroom_database, us, second, 2)

    async with newsroom_database() as database:
        await events_service.merge_events(
            database, target_id=target, source_ids=[first, second, first], user_id=user_id
        )
        await database.commit()

    async with newsroom_database() as database:
        rows = (await database.execute(select(NewsroomArticle.id, NewsroomArticle.event_id))).all()
    article_events = {row.id: row.event_id for row in rows}
    assert {article_events[a] for a in [kept, *moved]} == {target}
    for source in (first, second, older):
        merged = await _get(newsroom_database, NewsroomEvent, source)
        assert (merged.status, merged.merged_into_id) == ("merged", target)
    target_row = await _get(newsroom_database, NewsroomEvent, target)
    assert (target_row.status, target_row.analysis_status, target_row.analysis_attempts) == (
        "open",
        "pending",
        0,
    )
    dropped_row = await _get(newsroom_database, NewsroomEditionItem, dropped)
    assert (dropped_row.event_id, dropped_row.removed_at is not None) == (first, True)
    repointed_row = await _get(newsroom_database, NewsroomEditionItem, repointed)
    assert (repointed_row.event_id, repointed_row.removed_at) == (target, None)
    us_rows = [await _get(newsroom_database, NewsroomEditionItem, i) for i in (us_first, us_second)]
    assert [(r.event_id, r.removed_at is None) for r in us_rows] == [
        (target, True),
        (second, False),
    ]

    [merge_log] = await _edits(newsroom_database, target)
    assert (merge_log.action, merge_log.user_id) == ("merge", user_id)
    assert merge_log.before == {"source_event_ids": [str(first), str(second)]}
    merge_after = _logged(merge_log.after)
    assert sorted(merge_after["moved_article_ids"]) == sorted(str(a) for a in moved)
    assert merge_after["repointed_item_ids"] == [str(repointed), str(us_first)]
    assert merge_after["removed_item_ids"] == [str(dropped), str(us_second)]
    assert merge_after["analysis_requeued_event_ids"] == [str(target)]
    for source in (first, second):
        [source_log] = await _edits(newsroom_database, source)
        assert source_log.action == "merged_into"
        assert source_log.after == {"status": "merged", "merged_into_id": str(target)}


async def test_merge_leaves_an_unplaced_target_unqueued(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _user(newsroom_database)
    target = await _event(newsroom_database, "Target")
    source = await _event(newsroom_database, "Source")
    async with newsroom_database() as database:
        await events_service.merge_events(
            database, target_id=target, source_ids=[source], user_id=user_id
        )
        await database.commit()
    assert (await _get(newsroom_database, NewsroomEvent, target)).analysis_status == "idle"


async def test_merge_rejects_invalid_requests(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _user(newsroom_database)
    target = await _event(newsroom_database, "Target")
    other_window = await _event(newsroom_database, "Other", edition_date=WINDOW + timedelta(1))
    merged = await _event(newsroom_database, "Merged")
    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomEvent)
            .where(NewsroomEvent.id == merged)
            .values(status="merged", merged_into_id=target)
        )
        await database.commit()

    cases = [
        ([], "invalid_merge_sources"),
        ([target], "invalid_merge_sources"),
        ([uuid.uuid4()], "event_not_found"),
        ([other_window], "edition_date_mismatch"),
        ([merged], "event_not_open"),
    ]
    for sources, code in cases:
        async with newsroom_database() as database:
            with pytest.raises(EventServiceError) as raised:
                await events_service.merge_events(
                    database, target_id=target, source_ids=sources, user_id=user_id
                )
            await database.rollback()
        assert raised.value.code == code
    assert await _count(newsroom_database, NewsroomEditLog) == 0


async def test_split_opens_a_new_event_and_requeues_the_placed_original(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _user(newsroom_database)
    source_id = await _source(newsroom_database)
    original = await _event(newsroom_database, "Mixed story", analysis_status="ready")
    stays = await _article(newsroom_database, source_id, "Fed", angle=0, event_id=original)
    later = await _article(
        newsroom_database, source_id, "Oil later", angle=0, event_id=original, seen_minutes=9
    )
    earlier = await _article(
        newsroom_database, source_id, "Oil earlier", angle=0, event_id=original, seen_minutes=3
    )
    await _item(newsroom_database, await _edition(newsroom_database, "global"), original, 1)

    async with newsroom_database() as database:
        created = await events_service.split_event(
            database, event_id=original, article_ids=[later, earlier], user_id=user_id
        )
        await database.commit()

    new_event = await _get(newsroom_database, NewsroomEvent, created)
    assert (new_event.created_by, new_event.status, new_event.edition_date) == (
        "split",
        "open",
        WINDOW,
    )
    assert new_event.working_title == "Oil earlier"
    assert new_event.analysis_status == "idle"
    assert (await _get(newsroom_database, NewsroomEvent, original)).analysis_status == "pending"
    assert (await _get(newsroom_database, NewsroomArticle, stays)).event_id == original
    for moved in (later, earlier):
        assert (await _get(newsroom_database, NewsroomArticle, moved)).event_id == created

    [split_log] = await _edits(newsroom_database, original)
    assert (split_log.action, split_log.user_id) == ("split", user_id)
    split_after = _logged(split_log.after)
    assert sorted(_logged(split_log.before)["article_ids"]) == sorted(
        str(a) for a in (stays, later, earlier)
    )
    assert split_after["new_event_id"] == str(created)
    assert split_after["moved_article_ids"] == [str(later), str(earlier)]
    assert split_after["remaining_article_count"] == 1
    assert split_after["analysis_requeued_event_ids"] == [str(original)]
    [created_log] = await _edits(newsroom_database, created)
    assert created_log.action == "split_from"
    assert _logged(created_log.after)["source_event_id"] == str(original)


async def test_split_of_an_unplaced_event_leaves_analysis_idle(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _user(newsroom_database)
    source_id = await _source(newsroom_database)
    original = await _event(newsroom_database, "Story")
    await _article(newsroom_database, source_id, "a", event_id=original)
    moving = await _article(newsroom_database, source_id, "b", event_id=original)
    async with newsroom_database() as database:
        await events_service.split_event(
            database, event_id=original, article_ids=[moving], user_id=user_id
        )
        await database.commit()
    assert (await _get(newsroom_database, NewsroomEvent, original)).analysis_status == "idle"


async def test_split_rejects_invalid_requests(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _user(newsroom_database)
    source_id = await _source(newsroom_database)
    event = await _event(newsroom_database, "Story")
    only = await _article(newsroom_database, source_id, "a", event_id=event)
    stranger = await _article(newsroom_database, source_id, "b")

    cases = [
        (event, [], "empty_split"),
        (event, [stranger], "article_not_in_event"),
        (event, [only], "split_takes_every_article"),
        (uuid.uuid4(), [only], "event_not_found"),
    ]
    for event_id, articles, code in cases:
        async with newsroom_database() as database:
            with pytest.raises(EventServiceError) as raised:
                await events_service.split_event(
                    database, event_id=event_id, article_ids=articles, user_id=user_id
                )
            await database.rollback()
        assert raised.value.code == code
    assert await _count(newsroom_database, NewsroomEvent) == 1
