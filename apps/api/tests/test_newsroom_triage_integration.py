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
from daily_insights_api.modules.newsroom import clock, clustering, events_service, queue, triage
from daily_insights_api.modules.newsroom.contracts import EmbeddingTranslation, EventNew
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
from daily_insights_api.modules.newsroom.queue import (
    FatalStageError,
    RetryableStageError,
    claim,
    run_claimed,
)
from daily_insights_api.modules.newsroom.reembed import reembed_articles
from daily_insights_api.modules.newsroom.variants import (
    BASELINE,
    OLDNEWS,
    P0,
    P0B,
    SelectionVariant,
)
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


def _translated(payload: dict[str, Any]) -> dict[str, Any]:
    return {"title": f"譯 {payload['title']}", "summary": payload["summary"]}


@dataclass
class FakeJsonModel:
    reply: Reply
    payloads: list[dict[str, Any]] = field(default_factory=list)
    barrier: asyncio.Barrier | None = None
    # Answers the embed stage's translation of non-Chinese headlines.
    translate: Reply = _translated
    translations: list[dict[str, Any]] = field(default_factory=list)
    # The selection variant the runtime under test is configured with.
    variant: SelectionVariant = P0

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
        if result_type is EmbeddingTranslation:
            assert system == triage.load_embed_translate_prompt().text
            self.translations.append(payload)
            answer = self.translate(payload)
            if isinstance(answer, RetryableStageError | FatalStageError):
                answer.audit_rows.append(
                    NewsroomLlmCall(
                        stage=audit.stage,
                        subject_id=audit.subject_id,
                        model=model,
                        latency_ms=1,
                        error_code=answer.code,
                    )
                )
                raise answer
            database.add(
                NewsroomLlmCall(
                    stage=audit.stage,
                    subject_id=audit.subject_id,
                    model=model,
                    prompt_version=audit.prompt_version,
                    latency_ms=1,
                )
            )
            return result_type.model_validate(answer)
        assert system == triage.load_triage_prompt(variant=self.variant).text
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
        "topic": "policy",
        "story_type": "official",
        "new_facts": True,
        "impact": 4,
        "directness": {"global": 4, "tw_equity": 2, "us_equity": 3},
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
    variant: SelectionVariant = P0,
) -> Runtime:
    return Runtime(
        settings=Settings(
            newsroom_triage_model="triage-test-model",
            newsroom_translate_model="translate-test-model",
            newsroom_selection_variant=variant.name,
        ),
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
    embedder = FakeEmbedder({"譯 Fed holds rates": 10})
    llm = FakeJsonModel(lambda payload: relevant({"new": "unused"}))
    runtime = _runtime(newsroom_database, llm=llm, embedder=embedder)

    assert await _run(runtime, "embed") == ["done"]

    # A non-Chinese headline is embedded in Traditional Chinese.
    assert llm.translations == [
        {"title": "Fed holds rates", "summary": "The Fed kept rates unchanged."}
    ]
    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert embedder.texts == ["譯 Fed holds rates\nThe Fed kept rates unchanged."]
    assert row.embedding_text == embedder.texts[0]
    assert row.embed_status == "done"
    assert list(row.embedding) == pytest.approx(vector(10))
    assert (row.triage_status, row.triage_attempts, row.triage_next_attempt_at) == (
        "pending",
        0,
        None,
    )
    async with newsroom_database() as database:
        audits = (await database.scalars(select(NewsroomLlmCall))).all()
    assert sorted((audit.stage, audit.model) for audit in audits) == [
        ("embed", "e"),
        ("embed", "translate-test-model"),
    ]
    assert all(audit.subject_id == article_id for audit in audits)
    assert any((audit.prompt_version or "").startswith("embed-translate-v1:") for audit in audits)


async def test_chinese_headline_is_embedded_without_translation(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _article(
        newsroom_database, source_id, "台積電9月營收創新高", embed_status="pending"
    )
    embedder = FakeEmbedder({"台積電9月營收創新高": 10})
    llm = FakeJsonModel(lambda payload: relevant({"new": "unused"}))

    assert await _run(_runtime(newsroom_database, llm=llm, embedder=embedder), "embed") == ["done"]

    assert llm.translations == []
    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert row.embedding_text == "台積電9月營收創新高"


async def test_failed_translation_retries_then_embeds_the_original(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _article(newsroom_database, source_id, "Oil jumps", embed_status="pending")
    embedder = FakeEmbedder({"Oil jumps": 10})
    llm = FakeJsonModel(
        lambda payload: relevant({"new": "unused"}),
        translate=lambda payload: RetryableStageError("embed_provider_timeout"),
    )
    runtime = _runtime(newsroom_database, llm=llm, embedder=embedder)

    assert await _run(runtime, "embed") == ["pending"]
    assert embedder.texts == []

    # On the stage's last attempt the article is embedded as written.
    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomArticle)
            .where(NewsroomArticle.id == article_id)
            .values(embed_attempts=queue.EMBED.max_attempts - 1, embed_next_attempt_at=None)
        )
        await database.commit()
    assert await _run(runtime, "embed") == ["done"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert (row.embed_status, row.embedding_text) == ("done", "Oil jumps")
    async with newsroom_database() as database:
        failed = (
            await database.scalars(
                select(NewsroomLlmCall).where(NewsroomLlmCall.error_code.is_not(None))
            )
        ).all()
    assert [audit.error_code for audit in failed] == ["embed_provider_timeout"] * 2


async def test_reembed_replaces_vectors_of_the_chosen_editions_only(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    english = await _article(newsroom_database, source_id, "Fed holds rates", angle=0)
    chinese = await _article(newsroom_database, source_id, "央行升息", angle=0)
    earlier = await _article(
        newsroom_database,
        source_id,
        "Older story",
        angle=0,
        edition_date=WINDOW - timedelta(days=1),
    )
    unembedded = await _article(newsroom_database, source_id, "Not yet embedded")
    embedder = FakeEmbedder({"譯 Fed holds rates": 30, "央行升息": 60})
    llm = FakeJsonModel(lambda payload: relevant({"new": "unused"}))

    report = await reembed_articles(
        _runtime(newsroom_database, llm=llm, embedder=embedder), WINDOW, WINDOW
    )

    assert report.as_dict() == {"articles": 2, "translated": 1}
    assert [payload["title"] for payload in llm.translations] == ["Fed holds rates"]
    rows = {
        article_id: await _get(newsroom_database, NewsroomArticle, article_id)
        for article_id in (english, chinese, earlier, unembedded)
    }
    assert list(rows[english].embedding) == pytest.approx(vector(30))
    assert rows[english].embedding_text == "譯 Fed holds rates"
    assert list(rows[chinese].embedding) == pytest.approx(vector(60))
    assert list(rows[earlier].embedding) == pytest.approx(vector(0))
    assert rows[earlier].embedding_text is None
    assert rows[unembedded].embedding is None


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
    assert (row.relevant, row.topic, row.story_type) == (True, "policy", "official")
    # Impact 4 is 80 on the default (p0) scale, scaled by each market's directness.
    assert row.market_scores == {"global": 80, "tw_equity": 56, "us_equity": 68}
    assert row.triage_signals == {
        "impact": 4,
        "new_facts": True,
        "directness": {"global": 4, "tw_equity": 2, "us_equity": 3},
        "escalation": 0,
        "event_date": None,
        "stale": False,
    }
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
    assert audit.prompt_version == triage.load_triage_prompt(variant=P0).version
    assert audit.error_code is None


async def test_baseline_variant_triages_with_the_v2_prompt(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    await _queued_for_triage(newsroom_database, source_id, "Fed holds rates", 0)
    llm = FakeJsonModel(lambda payload: relevant({"new": "聯準會維持利率不變"}), variant=BASELINE)

    assert await _run(_runtime(newsroom_database, llm=llm, variant=BASELINE), "triage") == ["done"]

    async with newsroom_database() as database:
        audit = (await database.scalars(select(NewsroomLlmCall))).one()
    assert audit.prompt_version is not None
    assert audit.prompt_version.startswith("triage-v2:")


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
    # More open events than the limit: only the nearest ones are offered.
    limit = clustering.CANDIDATE_EVENT_LIMIT
    others = [await _event(newsroom_database, f"Other {n}") for n in range(limit + 1)]
    for n, other in enumerate(others):
        await _article(newsroom_database, minor, f"Other {n}", angle=20 + 5 * n, event_id=other)
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
    assert [c["id"] for c in candidates] == [str(fed), *(str(o) for o in others[: limit - 1])]
    assert candidates[0] == {
        "id": str(fed),
        "working_title": "Fed decision",
        "titles": ["Trusted: Fed", "Minor: Fed"],
    }
    assert candidates[1]["titles"] == ["Other 0"]
    assert (await _get(newsroom_database, NewsroomArticle, article_id)).event_id == fed
    assert (
        await _count(newsroom_database, NewsroomEvent) == limit + 4
    )  # fed, the others, yesterday and merged


async def test_irrelevant_article_is_scored_but_not_clustered(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _queued_for_triage(newsroom_database, source_id, "Celebrity wedding", 0)
    llm = FakeJsonModel(
        lambda payload: {
            "topic": "companies",
            "story_type": "other",
            "new_facts": False,
            "impact": 4,
            "directness": {"global": 0, "tw_equity": 1, "us_equity": 0},
            "event": {"new": "should be ignored"},
        }
    )

    assert await _run(_runtime(newsroom_database, llm=llm), "triage") == ["done"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert (row.triage_status, row.relevant, row.event_id) == ("done", False, None)
    assert row.market_scores == {"global": 0, "tw_equity": 13, "us_equity": 0}
    assert await _count(newsroom_database, NewsroomEvent) == 0


async def test_price_action_is_capped_by_code_whatever_the_model_judged(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _queued_for_triage(newsroom_database, source_id, "台股開盤重挫逾 500 點", 0)
    llm = FakeJsonModel(
        lambda payload: relevant({"new": "台股開盤重挫逾 500 點"}) | {"story_type": "price_action"}
    )

    assert await _run(_runtime(newsroom_database, llm=llm), "triage") == ["done"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert row.story_type == "price_action"
    assert row.market_scores == {"global": 40, "tw_equity": 40, "us_equity": 40}
    # Still clustered, so later reports of the same move join it instead of opening more.
    assert (row.relevant, row.event_id is not None) == (True, True)


# The window of ``WINDOW`` opens 2026-09-30 08:00 Taipei (00:00 UTC).
PUBLISHED_LONG_BEFORE = datetime(2026, 9, 29, 12, tzinfo=UTC)


async def test_article_published_before_the_window_cannot_open_an_event(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _queued_for_triage(
        newsroom_database, source_id, "Yesterday's revenue", 0, published_at=PUBLISHED_LONG_BEFORE
    )
    llm = FakeJsonModel(lambda payload: relevant({"new": "鴻海 9 月營收"}))

    assert await _run(_runtime(newsroom_database, llm=llm), "triage") == ["done"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert (row.triage_status, row.relevant, row.event_id) == ("done", True, None)
    assert row.triage_signals is not None and row.triage_signals["stale"] is True
    assert await _count(newsroom_database, NewsroomEvent) == 0


async def test_article_published_before_the_window_still_joins_an_event(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    existing = await _event(newsroom_database, "鴻海 9 月營收")
    await _article(newsroom_database, source_id, "Revenue", angle=0, event_id=existing)
    article_id = await _queued_for_triage(
        newsroom_database, source_id, "Revenue again", 1, published_at=PUBLISHED_LONG_BEFORE
    )
    llm = FakeJsonModel(lambda payload: relevant({"match": payload["candidate_events"][0]["id"]}))

    assert await _run(_runtime(newsroom_database, llm=llm), "triage") == ["done"]

    assert (await _get(newsroom_database, NewsroomArticle, article_id)).event_id == existing


async def test_manual_article_opens_an_event_however_old_it_is(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        manual = NewsroomSource(
            key="manual", name="Manual", kind="manual", hostname="manual", markets=["global"]
        )
        database.add(manual)
        await database.commit()
        manual_id = manual.id
    article_id = await _queued_for_triage(
        newsroom_database, manual_id, "Admin pick", 0, published_at=PUBLISHED_LONG_BEFORE
    )
    llm = FakeJsonModel(lambda payload: relevant({"new": "管理員加入的舊文"}))

    assert await _run(_runtime(newsroom_database, llm=llm), "triage") == ["done"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert row.event_id is not None
    assert row.triage_signals is not None and row.triage_signals["stale"] is False


# Under the event-date rule the window of ``WINDOW`` (opening 2026-09-30) holds
# back event dates of 2026-09-28 and earlier.
OLD_EVENT_DATE = "2026-09-28"


def _dated(event: dict[str, str], event_date: str | None, **extra: Any) -> dict[str, Any]:
    return relevant(event) | {"event_date": event_date} | extra


async def test_old_event_date_cannot_open_an_event_and_has_no_new_facts(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _queued_for_triage(
        newsroom_database,
        source_id,
        "Weekly recap",
        0,
        published_at=datetime(2026, 9, 30, 3, 20, tzinfo=UTC),
    )
    llm = FakeJsonModel(
        lambda payload: _dated({"new": "美國就業數據"}, OLD_EVENT_DATE), variant=OLDNEWS
    )

    assert await _run(_runtime(newsroom_database, llm=llm, variant=OLDNEWS), "triage") == ["done"]

    [payload] = llm.payloads
    assert payload["article"]["published_at"] == "2026-09-30T11:20:00+08:00"
    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert (row.relevant, row.event_id) == (True, None)
    # Scored as no new facts: impact 4 is 80 on the p0 scale, less the penalty.
    assert row.market_scores == {"global": 65, "tw_equity": 41, "us_equity": 53}
    # The raw answer is kept; the flag says the rule applied.
    assert row.triage_signals is not None
    assert row.triage_signals["new_facts"] is True
    assert row.triage_signals["event_date"] == OLD_EVENT_DATE
    assert (row.triage_signals["stale"], row.triage_signals["dated_old"]) == (False, True)
    assert await _count(newsroom_database, NewsroomEvent) == 0


async def test_old_event_date_still_joins_an_event(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    existing = await _event(newsroom_database, "美國就業數據")
    await _article(newsroom_database, source_id, "Jobs", angle=0, event_id=existing)
    article_id = await _queued_for_triage(newsroom_database, source_id, "Jobs recap", 1)
    llm = FakeJsonModel(
        lambda payload: _dated({"match": payload["candidate_events"][0]["id"]}, OLD_EVENT_DATE),
        variant=OLDNEWS,
    )

    assert await _run(_runtime(newsroom_database, llm=llm, variant=OLDNEWS), "triage") == ["done"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert row.event_id == existing
    assert row.market_scores["global"] == 65


@pytest.mark.parametrize(
    ("event_date", "story_type"),
    [
        # One day before the window opens is within the slack.
        ("2026-09-29", "data"),
        # A preview, or an article the model could not date, is never held back.
        (OLD_EVENT_DATE, "preview"),
        (None, "data"),
        ("2026-10-08", "data"),
    ],
)
async def test_event_date_rule_leaves_recent_undated_and_preview_articles_alone(
    newsroom_database: async_sessionmaker[AsyncSession], event_date: str | None, story_type: str
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _queued_for_triage(newsroom_database, source_id, "Jobs", 0)
    llm = FakeJsonModel(
        lambda payload: _dated({"new": "美國就業數據"}, event_date, story_type=story_type),
        variant=OLDNEWS,
    )

    assert await _run(_runtime(newsroom_database, llm=llm, variant=OLDNEWS), "triage") == ["done"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert row.event_id is not None
    assert row.triage_signals is not None and row.triage_signals["dated_old"] is False
    assert row.triage_signals["new_facts"] is True


async def test_event_date_is_ignored_without_the_rule(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    article_id = await _queued_for_triage(
        newsroom_database,
        source_id,
        "Weekly recap",
        0,
        published_at=datetime(2026, 9, 30, 3, 20, tzinfo=UTC),
    )
    llm = FakeJsonModel(
        lambda payload: _dated({"new": "美國就業數據"}, OLD_EVENT_DATE), variant=P0B
    )

    assert await _run(_runtime(newsroom_database, llm=llm, variant=P0B), "triage") == ["done"]

    [payload] = llm.payloads
    assert "published_at" not in payload["article"]
    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert row.event_id is not None
    assert row.market_scores == {"global": 80, "tw_equity": 56, "us_equity": 68}
    assert row.triage_signals is not None and "dated_old" not in row.triage_signals
    assert row.triage_signals["event_date"] == OLD_EVENT_DATE


async def test_manual_article_opens_an_event_whatever_its_event_date(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        manual = NewsroomSource(
            key="manual", name="Manual", kind="manual", hostname="manual", markets=["global"]
        )
        database.add(manual)
        await database.commit()
        manual_id = manual.id
    article_id = await _queued_for_triage(newsroom_database, manual_id, "Admin pick", 0)
    llm = FakeJsonModel(
        lambda payload: _dated({"new": "管理員加入的舊文"}, OLD_EVENT_DATE), variant=OLDNEWS
    )

    assert await _run(_runtime(newsroom_database, llm=llm, variant=OLDNEWS), "triage") == ["done"]

    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert row.event_id is not None
    assert row.market_scores["global"] == 80
    assert row.triage_signals is not None and row.triage_signals["dated_old"] is False


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
            known_event_ids={target, absorbed},
        )
        await database.rollback()
    assert resolved == target


async def test_new_event_choice_joins_only_an_event_opened_during_the_call(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    existing = await _event(newsroom_database, "Fed decision")
    await _article(newsroom_database, source_id, "Fed", angle=0, event_id=existing)
    article_id = await _article(newsroom_database, source_id, "Fed again", angle=20)
    async with newsroom_database() as database:
        # cos(20°) ≈ 0.94 is above the threshold: an event the model never saw is joined.
        joined = await triage.assign_event(
            database,
            article_id=article_id,
            edition_date=WINDOW,
            embedding=vector(20),
            choice=EventNew(new="Fed again"),
            known_event_ids=set(),
        )
        # The same event, already known before the call, does not override the model.
        kept_apart = await triage.assign_event(
            database,
            article_id=article_id,
            edition_date=WINDOW,
            embedding=vector(20),
            choice=EventNew(new="Fed again"),
            known_event_ids={existing},
        )
        assert kept_apart is not None
        far = await triage.assign_event(
            database,
            article_id=article_id,
            edition_date=WINDOW,
            embedding=vector(40),
            choice=EventNew(new="Something else"),
            known_event_ids={existing, kept_apart},
        )
        await database.rollback()
    assert joined == existing
    assert kept_apart != existing
    assert far not in {existing, kept_apart}


async def test_new_event_choice_overrides_no_offered_candidate(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    offered = await _event(newsroom_database, "Fed decision")
    await _article(newsroom_database, source_id, "Fed", angle=0, event_id=offered)
    article_id = await _queued_for_triage(newsroom_database, source_id, "Fed outlook", 10)
    llm = FakeJsonModel(lambda payload: relevant({"new": "聯準會官員談利率展望"}))

    assert await _run(_runtime(newsroom_database, llm=llm), "triage") == ["done"]

    assert [c["id"] for c in llm.payloads[0]["candidate_events"]] == [str(offered)]
    row = await _get(newsroom_database, NewsroomArticle, article_id)
    assert row.event_id is not None and row.event_id != offered
    assert (await _get(newsroom_database, NewsroomEvent, row.event_id)).working_title == (
        "聯準會官員談利率展望"
    )


async def test_new_event_choice_ignores_events_that_predate_the_call(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await _source(newsroom_database)
    # Near-identical events beyond the limit: one existed but was not offered.
    limit = clustering.CANDIDATE_EVENT_LIMIT
    events = [await _event(newsroom_database, f"Story {n}") for n in range(limit + 1)]
    for n, event_id in enumerate(events):
        await _article(newsroom_database, source_id, f"Story {n}", angle=1 + n, event_id=event_id)
    await _queued_for_triage(newsroom_database, source_id, "Story again", 0)
    llm = FakeJsonModel(lambda payload: relevant({"new": "Another story"}))

    assert await _run(_runtime(newsroom_database, llm=llm), "triage") == ["done"]

    assert len(llm.payloads[0]["candidate_events"]) == limit
    assert await _count(newsroom_database, NewsroomEvent) == limit + 2


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
    assert merge_after["restored_item_ids"] == []
    assert merge_after["moved_snapshot_urls"] == []
    assert merge_after["analysis_requeued_event_ids"] == [str(target)]
    for source in (first, second):
        [source_log] = await _edits(newsroom_database, source)
        assert source_log.action == "merged_into"
        assert source_log.after == {"status": "merged", "merged_into_id": str(target)}


async def test_merge_restores_a_removed_target_item_in_the_live_source_slot(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _user(newsroom_database)
    target = await _event(newsroom_database, "Target")
    first = await _event(newsroom_database, "First")
    second = await _event(newsroom_database, "Second")
    removed_at = datetime(2026, 9, 30, 23, tzinfo=UTC)
    global_ = await _edition(newsroom_database, "global")
    tw = await _edition(newsroom_database, "tw_equity")
    # global: removed target, live source → the target comes back at rank 2.
    revived = await _item(newsroom_database, global_, target, 4, removed_at=removed_at)
    replaced = await _item(newsroom_database, global_, first, 2)
    # tw: removed target, removed source → nothing changes.
    still_removed = await _item(newsroom_database, tw, target, 3, removed_at=removed_at)
    untouched = await _item(newsroom_database, tw, second, 1, removed_at=removed_at)

    async with newsroom_database() as database:
        await events_service.merge_events(
            database, target_id=target, source_ids=[first, second], user_id=user_id
        )
        await database.commit()

    revived_row = await _get(newsroom_database, NewsroomEditionItem, revived)
    assert (revived_row.event_id, revived_row.rank, revived_row.removed_at) == (target, 2, None)
    replaced_row = await _get(newsroom_database, NewsroomEditionItem, replaced)
    assert (replaced_row.event_id, replaced_row.removed_at is not None) == (first, True)
    still_removed_row = await _get(newsroom_database, NewsroomEditionItem, still_removed)
    assert (still_removed_row.rank, still_removed_row.removed_at) == (3, removed_at)
    untouched_row = await _get(newsroom_database, NewsroomEditionItem, untouched)
    assert (untouched_row.event_id, untouched_row.removed_at) == (second, removed_at)
    assert (await _get(newsroom_database, NewsroomEvent, target)).analysis_status == "pending"

    [merge_log] = await _edits(newsroom_database, target)
    merge_after = _logged(merge_log.after)
    assert merge_after["restored_item_ids"] == [str(revived)]
    assert merge_after["removed_item_ids"] == [str(replaced)]
    assert merge_after["repointed_item_ids"] == []


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


async def test_split_moves_matching_snapshots_and_keeps_snapshot_only_sources(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _user(newsroom_database)
    source_id = await _source(newsroom_database)
    original = await _event(newsroom_database, "Merged history")
    moving = await _article(newsroom_database, source_id, "Moving", event_id=original)
    staying = await _article(newsroom_database, source_id, "Staying", event_id=original)
    moving_url = (await _get(newsroom_database, NewsroomArticle, moving)).url
    staying_url = (await _get(newsroom_database, NewsroomArticle, staying)).url
    snapshots = [
        {"name": name, "url": url, "published_at": None}
        for name, url in [
            ("Moving publisher", moving_url),
            ("Staying publisher", staying_url),
            ("Snapshot only", "https://example.com/older-article"),
        ]
    ]
    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomEvent)
            .where(NewsroomEvent.id == original)
            .values(legacy_sources=snapshots)
        )
        created = await events_service.split_event(
            database, event_id=original, article_ids=[moving], user_id=user_id
        )
        await database.commit()
    assert (await _get(newsroom_database, NewsroomEvent, original)).legacy_sources == snapshots[1:]
    assert (await _get(newsroom_database, NewsroomEvent, created)).legacy_sources == snapshots[:1]
    [split_log] = await _edits(newsroom_database, original)
    assert _logged(split_log.after)["moved_snapshot_urls"] == [moving_url]
    [created_log] = await _edits(newsroom_database, created)
    assert _logged(created_log.after)["snapshot_urls"] == [moving_url]


async def test_merge_log_lists_the_snapshot_links_the_target_gained(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await _user(newsroom_database)
    shared = {"name": "Shared", "url": "https://example.com/shared", "published_at": None}
    only = {"name": "Only", "url": "https://example.com/only", "published_at": None}
    target = await _event(newsroom_database, "Target")
    incoming = await _event(newsroom_database, "Incoming")
    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomEvent).where(NewsroomEvent.id == target).values(legacy_sources=[shared])
        )
        await database.execute(
            update(NewsroomEvent)
            .where(NewsroomEvent.id == incoming)
            .values(legacy_sources=[{**shared, "name": "Duplicate"}, only])
        )
        await database.commit()
    async with newsroom_database() as database:
        await events_service.merge_events(
            database, target_id=target, source_ids=[incoming], user_id=user_id
        )
        await database.commit()
    assert (await _get(newsroom_database, NewsroomEvent, target)).legacy_sources == [shared, only]
    [merge_log] = await _edits(newsroom_database, target)
    assert _logged(merge_log.after)["moved_snapshot_urls"] == [only["url"]]


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
