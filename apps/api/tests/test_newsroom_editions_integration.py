import math
import os
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.models import Base
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.newsroom import (
    analysis,
    assembly,
    clock,
    publishing,
    queue,
    translation,
)
from daily_insights_api.modules.newsroom.contracts import (
    AnalysisResult,
    DuplicateGroups,
    EditorResult,
    PublishedMatches,
    TranslationResult,
    WhyResult,
)
from daily_insights_api.modules.newsroom.models import (
    EMBEDDING_DIMENSIONS,
    NewsroomArticle,
    NewsroomDedupeDecision,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEditLog,
    NewsroomEditorRating,
    NewsroomEvent,
    NewsroomLlmCall,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.notifier import LogNotifier
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.queue import (
    RetryableStageError,
    StageSpec,
    claim,
    run_claimed,
)
from daily_insights_api.modules.newsroom.variants import BASELINE, OLDNEWS, P0, VariantName
from daily_insights_api.modules.newsroom.worker import Runtime, StageBinding

pytestmark = pytest.mark.integration

EDITION_DATE = date(2026, 9, 30)
MARKETS = ("global", "tw_equity", "us_equity")

Responder = Callable[[dict[str, Any]], Any]


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


@dataclass
class FakeLlm:
    """Answers per result type; a responder may return an exception to raise."""

    responders: dict[type[BaseModel], Responder] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # System prompt per call, by stage.
    systems: list[tuple[str, str]] = field(default_factory=list)

    async def complete(
        self,
        database: AsyncSession,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        result_type: type[Any],
        audit: CallAudit,
    ) -> Any:
        assert system
        if result_type in (DuplicateGroups, PublishedMatches) and (
            result_type not in self.responders
        ):
            # Tests that do not exercise the duplicate screens see no duplicates.
            return result_type()
        self.calls.append((audit.stage, payload))
        self.systems.append((audit.stage, system))
        response = self.responders[result_type](payload)
        if isinstance(response, Exception):
            raise response
        database.add(
            NewsroomLlmCall(
                stage=audit.stage,
                subject_id=audit.subject_id,
                model=model,
                prompt_version=audit.prompt_version,
                latency_ms=1,
            )
        )
        return result_type.model_validate(response)

    def stages(self) -> list[str]:
        return [stage for stage, _ in self.calls]


class NoEmbedder:
    async def embed(self, *args: Any, **kwargs: Any) -> list[list[float]]:
        raise AssertionError("not used")


def _runtime(
    session_factory: async_sessionmaker[AsyncSession],
    llm: FakeLlm,
    embedder: Any = None,
    *,
    variant: VariantName = "p0",
) -> Runtime:
    return Runtime(
        # Expectations below are p0 scores; the production default is baseline.
        settings=Settings(
            newsroom_enabled=True,
            newsroom_admin_base_url="https://admin.test/",
            newsroom_selection_variant=variant,
        ),
        session_factory=session_factory,
        llm=llm,
        embedder=embedder or NoEmbedder(),
        notifier=LogNotifier(),
    )


class TitleEmbedder:
    """Unit vectors per working title; titles sharing a prefix point the same way."""

    def __init__(self, directions: dict[str, list[float]]) -> None:
        self.directions = directions

    async def embed(
        self, database: AsyncSession, texts: Sequence[str], *, audit: CallAudit
    ) -> list[list[float]]:
        del database, audit
        return [next(v for k, v in self.directions.items() if text.startswith(k)) for text in texts]


def _notices(runtime: Runtime) -> LogNotifier:
    assert isinstance(runtime.notifier, LogNotifier)
    return runtime.notifier


async def _no_sleep(seconds: float) -> None:
    del seconds


# --- Seeding ------------------------------------------------------------------------


async def _user(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with session_factory() as database:
        database.add(
            User(
                id=user_id,
                email=f"editor-{user_id}@example.com",
                display_name="Editor",
                password_hash="unused",
                must_change_password=False,
                system_role=SystemRole.ADMIN,
                status=UserStatus.ACTIVE,
            )
        )
        await database.commit()
    return user_id


async def _source(
    session_factory: async_sessionmaker[AsyncSession],
    key: str,
    *,
    weight: float = 1.0,
    trust_tier: int = 2,
    name: str | None = None,
) -> uuid.UUID:
    async with session_factory() as database:
        source = NewsroomSource(
            key=key,
            name=name or key.title(),
            kind="rss",
            hostname=f"{key}.example.com",
            markets=["global"],
            weight=weight,
            trust_tier=trust_tier,
        )
        database.add(source)
        await database.commit()
        return source.id


async def _event(
    session_factory: async_sessionmaker[AsyncSession],
    title: str,
    *,
    sources: list[uuid.UUID],
    scores: dict[str, int],
    body: bool = True,
    relevant: bool = True,
    edition_date: date = EDITION_DATE,
    story_types: Sequence[str | None] = (),
    seen_minutes: int = 0,
) -> uuid.UUID:
    """One article per source; ``story_types`` lines up with ``sources`` (default untyped)."""
    async with session_factory() as database:
        event = NewsroomEvent(edition_date=edition_date, working_title=title)
        database.add(event)
        await database.flush()
        for index, source_id in enumerate(sources):
            url = f"https://example.com/{uuid.uuid4()}"
            database.add(
                NewsroomArticle(
                    source_id=source_id,
                    url=url,
                    url_hash=uuid.uuid4().hex * 2,
                    title=f"{title} report {index}",
                    edition_date=edition_date,
                    first_seen_at=datetime(2026, 9, 29, 1, tzinfo=UTC)
                    + timedelta(minutes=seen_minutes),
                    story_type=story_types[index] if index < len(story_types) else None,
                    body=f"{title} 全文 {index}" if body and index == 0 else None,
                    body_status="ok" if body and index == 0 else "unavailable",
                    fetch_status="done",
                    embed_status="done",
                    triage_status="done",
                    relevant=relevant,
                    topic="markets",
                    market_scores=scores,
                    event_id=event.id,
                )
            )
        await database.commit()
        return event.id


async def _editions(
    session_factory: async_sessionmaker[AsyncSession], edition_date: date = EDITION_DATE
) -> dict[str, NewsroomEdition]:
    async with session_factory() as database:
        rows = (
            await database.scalars(
                select(NewsroomEdition).where(NewsroomEdition.edition_date == edition_date)
            )
        ).all()
        return {row.market_code: row for row in rows}


async def _items(
    session_factory: async_sessionmaker[AsyncSession], edition_id: uuid.UUID
) -> list[NewsroomEditionItem]:
    async with session_factory() as database:
        return list(
            (
                await database.scalars(
                    select(NewsroomEditionItem)
                    .where(NewsroomEditionItem.edition_id == edition_id)
                    .order_by(NewsroomEditionItem.rank)
                )
            ).all()
        )


async def _get(session_factory: async_sessionmaker[AsyncSession], model: Any, row_id: Any) -> Any:
    async with session_factory() as database:
        row = await database.get(model, row_id)
        assert row is not None
        return row


def _stars_by_title(stars: dict[str, int], default: int = 1) -> Responder:
    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "ratings": [
                {"event_id": event["event_id"], "stars": stars.get(event["working_title"], default)}
                for event in payload["events"]
            ]
        }

    return respond


async def _run_stage(
    runtime: Runtime, binding: StageBinding, *, now: datetime | None = None
) -> list[str]:
    outcomes: list[str] = []
    while True:
        async with runtime.session_factory() as database:
            claims = await claim(
                database, binding.stage, extra_filter=binding.extra_filter, now=now
            )
        if not claims:
            return outcomes
        for item in claims:
            outcomes.append(await run_claimed(runtime.session_factory, item, binding.handler))


def _binding(registration_stages: list[StageBinding], stage: StageSpec) -> StageBinding:
    return next(binding for binding in registration_stages if binding.stage is stage)


# --- Assembly -----------------------------------------------------------------------


async def test_assembly_selects_by_quota_and_queues_analysis(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    reuters = await _source(newsroom_database, "reuters", weight=1.5)
    cna = await _source(newsroom_database, "cna")
    titles = [f"global-{index}" for index in range(8)]
    for index, title in enumerate(titles):
        await _event(
            newsroom_database,
            title,
            sources=[reuters, cna] if index == 0 else [cna],
            scores={"global": 50 + index, "tw_equity": 0, "us_equity": 10},
        )
    no_body = await _event(
        newsroom_database, "no-body", sources=[cna], scores={"global": 99}, body=False
    )
    irrelevant = await _event(
        newsroom_database, "irrelevant", sources=[cna], scores={"global": 99}, relevant=False
    )
    llm = FakeLlm(
        {
            EditorResult: _stars_by_title(
                {"global-0": 5, "global-7": 5, "global-3": 4, "global-5": 3, "global-6": 2}
            )
        }
    )
    runtime = _runtime(newsroom_database, llm)

    report = await assembly.assemble_editions(runtime, EDITION_DATE, sleep=_no_sleep)

    assert [market.action for market in report.markets] == ["assembled"] * 3
    editions = await _editions(newsroom_database)
    assert set(editions) == set(MARKETS)
    global_edition = editions["global"]
    assert global_edition.status == "draft"
    assert global_edition.selection_mode == "editor"
    assert global_edition.auto_publish_at == clock.auto_publish_at(EDITION_DATE)
    assert global_edition.late_fill_deadline == clock.late_fill_deadline(EDITION_DATE)
    items = await _items(newsroom_database, global_edition.id)
    async with newsroom_database() as database:
        event_titles = {
            event.id: event.working_title
            for event in (await database.scalars(select(NewsroomEvent))).all()
        }
    # Two 5-star and one 4-star leave room for two fillers, but only 3-star
    # events fill: the edition runs short rather than carry 1-2 star events.
    assert [(event_titles[item.event_id], item.stars) for item in items] == [
        ("global-0", 5),
        ("global-7", 5),
        ("global-3", 4),
        ("global-5", 3),
    ]
    assert [item.rank for item in items] == [1, 2, 3, 4]
    # Score 50 with the 1.5 weight clamped to 1.1, plus p0's bonus for two outlets.
    assert items[0].editor_score == pytest.approx(61.0)
    assert all(item.why_status == "pending" and item.origin == "model" for item in items)
    # tw_equity has no event scored above zero for it.
    assert await _items(newsroom_database, editions["tw_equity"].id) == []
    for event_id in (no_body, irrelevant):
        assert (await _get(newsroom_database, NewsroomEvent, event_id)).analysis_status == "idle"
    selected = await _get(newsroom_database, NewsroomEvent, items[0].event_id)
    assert selected.analysis_status == "pending"
    editor_payload = llm.calls[0][1]
    assert editor_payload["market"] == "global"
    assert {event["working_title"] for event in editor_payload["events"]} == set(titles)
    assert editor_payload["events"][0]["headlines"][0]["source"] == "Reuters"
    notices = _notices(runtime).sent
    assert [notice.kind for notice in notices] == ["draft_ready"]
    assert notices[0].link == "https://admin.test/admin/newsroom?date=2026-09-30"
    assert "5 星 global: global-0" in notices[0].lines
    # Every rated candidate keeps its stars, picked or not.
    async with newsroom_database() as database:
        ratings = (
            await database.scalars(
                select(NewsroomEditorRating)
                .where(NewsroomEditorRating.market_code == "global")
                .order_by(NewsroomEditorRating.position)
            )
        ).all()
    assert [(row.event_title, row.stars, row.picked) for row in ratings] == [
        ("global-0", 5, True),
        ("global-7", 5, True),
        ("global-6", 2, False),
        ("global-5", 3, True),
        ("global-4", 1, False),
        ("global-3", 4, True),
        ("global-2", 1, False),
        ("global-1", 1, False),
    ]
    assert [row.position for row in ratings] == list(range(1, 9))
    assert ratings[0].score == pytest.approx(61.0)
    assert all(
        row.prompt_version == assembly.editor_prompt_version(variant=P0) and not row.follow_up
        for row in ratings
    )


async def test_assembly_rebuild_keeps_admin_actions_and_skips_published(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source = await _source(newsroom_database, "cna")
    events = {
        title: await _event(
            newsroom_database,
            title,
            sources=[source],
            scores={"global": score, "tw_equity": score, "us_equity": score},
        )
        for title, score in (("a", 90), ("b", 80), ("c", 70), ("d", 60), ("e", 50), ("f", 40))
    }
    extra = await _event(newsroom_database, "extra", sources=[source], scores={"global": 0})
    user_id = await _user(newsroom_database)
    llm = FakeLlm({EditorResult: _stars_by_title({"a": 4, "b": 4, "c": 4, "d": 4, "e": 4})})
    runtime = _runtime(newsroom_database, llm)
    await assembly.assemble_editions(runtime, EDITION_DATE, sleep=_no_sleep)
    editions = await _editions(newsroom_database)
    global_id = editions["global"].id
    first = await _items(newsroom_database, global_id)
    removed = next(item for item in first if item.event_id == events["b"])

    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomEditionItem)
            .where(NewsroomEditionItem.id == removed.id)
            .values(removed_at=datetime.now(UTC))
        )
        manual_id = await publishing.add_event_to_edition(
            database, global_id, extra, user_id=user_id
        )
        await publishing.publish_edition(database, editions["us_equity"].id, user_id=user_id)
        await database.commit()
    published_before = await _items(newsroom_database, editions["us_equity"].id)

    # Second pass: the editor now prefers f over c, d and e.
    llm.responders[EditorResult] = _stars_by_title({"a": 5, "b": 5, "f": 4, "c": 3, "d": 3})
    report = await assembly.assemble_editions(runtime, EDITION_DATE, sleep=_no_sleep)

    assert {market.market: market.action for market in report.markets} == {
        "global": "assembled",
        "tw_equity": "assembled",
        "us_equity": "skipped_published",
    }
    second = await _items(newsroom_database, global_id)
    by_event = {item.event_id: item for item in second}
    assert set(by_event) == {events[key] for key in "abcdf"} | {extra}
    assert by_event[events["b"]].removed_at is not None
    assert by_event[events["b"]].id == removed.id
    assert by_event[extra].id == manual_id
    assert by_event[extra].origin == "manual"
    # Model picks first (a, b at 5 stars, f at 4, then 3-star fillers c and d),
    # then manual items, then removed ones; e is no longer picked and is dropped.
    assert [item.event_id for item in second] == [
        events["a"],
        events["f"],
        events["c"],
        events["d"],
        extra,
        events["b"],
    ]
    assert [item.rank for item in second] == [1, 2, 3, 4, 5, 6]
    assert by_event[events["a"]].stars == 5
    published_after = await _items(newsroom_database, editions["us_equity"].id)
    assert [(item.id, item.rank) for item in published_after] == [
        (item.id, item.rank) for item in published_before
    ]
    async with newsroom_database() as database:
        count = len((await database.scalars(select(NewsroomEdition))).all())
    assert count == 3


async def test_editor_contract_errors_retry_then_fall_back(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source = await _source(newsroom_database, "cna")
    for index in range(7):
        await _event(
            newsroom_database, f"e{index}", sources=[source], scores={"global": 10 + index}
        )

    def missing_one(payload: dict[str, Any]) -> dict[str, Any]:
        events = payload["events"][1:]
        return {"ratings": [{"event_id": event["event_id"], "stars": 5} for event in events]}

    llm = FakeLlm({EditorResult: missing_one})
    runtime = _runtime(newsroom_database, llm)
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    await assembly.assemble_editions(runtime, EDITION_DATE, sleep=record_sleep)

    assert llm.stages() == ["editor"] * assembly.EDITOR_ATTEMPTS
    assert sleeps == [5.0] * (assembly.EDITOR_ATTEMPTS - 1)
    edition = (await _editions(newsroom_database))["global"]
    assert edition.selection_mode == "fallback"
    items = await _items(newsroom_database, edition.id)
    assert len(items) == assembly.FALLBACK_LIMIT
    assert all(item.stars is None for item in items)
    assert [item.editor_score for item in items] == [16.0, 15.0, 14.0, 13.0, 12.0]
    kinds = [notice.kind for notice in _notices(runtime).sent]
    assert kinds == ["selection_fallback", "draft_ready"]


async def test_editor_extra_id_is_retried_and_then_accepted(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source = await _source(newsroom_database, "cna")
    await _event(newsroom_database, "only", sources=[source], scores={"global": 50})
    answers = iter(
        [
            {"ratings": [{"event_id": str(uuid.uuid4()), "stars": 5}]},
            None,
        ]
    )
    rate = _stars_by_title({"only": 5})

    def respond(payload: dict[str, Any]) -> Any:
        answer = next(answers)
        return answer if answer is not None else rate(payload)

    llm = FakeLlm({EditorResult: respond})
    runtime = _runtime(newsroom_database, llm)
    await assembly.assemble_editions(
        runtime, EDITION_DATE, sleep=_no_sleep, retry_delay=timedelta(0)
    )

    edition = (await _editions(newsroom_database))["global"]
    assert edition.selection_mode == "editor"
    assert [item.stars for item in await _items(newsroom_database, edition.id)] == [5]
    assert llm.stages() == ["editor", "editor"]
    async with newsroom_database() as database:
        audits = (
            await database.scalars(select(NewsroomLlmCall).order_by(NewsroomLlmCall.created_at))
        ).all()
    assert sorted(audit.error_code or "" for audit in audits) == ["", "editor_schema_invalid"]


async def test_assembly_waits_for_triage_then_ignores_the_rest(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source = await _source(newsroom_database, "cna")
    async with newsroom_database() as database:
        database.add(
            NewsroomArticle(
                source_id=source,
                url="https://example.com/late",
                url_hash="f" * 64,
                title="Late",
                edition_date=EDITION_DATE,
                triage_status="pending",
            )
        )
        await database.commit()
    moment = clock.window(EDITION_DATE)[1]
    sleeps: list[float] = []

    def now() -> datetime:
        return moment

    async def advance(seconds: float) -> None:
        nonlocal moment
        sleeps.append(seconds)
        moment += timedelta(seconds=seconds)

    runtime = _runtime(newsroom_database, FakeLlm())
    report = await assembly.assemble_editions(runtime, EDITION_DATE, now=now, sleep=advance)

    assert report.ignored_pending_triage == 1
    assert sum(sleeps) == clock.TRIAGE_GRACE.total_seconds()
    editions = await _editions(newsroom_database)
    assert all(edition.ignored_pending_triage == 1 for edition in editions.values())
    assert all(edition.assembled_at == moment for edition in editions.values())


# --- Analysis and WHY ---------------------------------------------------------------


async def _placed_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    markets: tuple[str, ...] = ("global", "tw_equity"),
    body: bool = True,
    published: bool = False,
    edition_date: date = EDITION_DATE,
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    source = await _source(session_factory, f"src-{uuid.uuid4().hex[:8]}")
    event_id = await _event(
        session_factory,
        "Fed holds",
        sources=[source],
        scores={"global": 80},
        body=body,
        edition_date=edition_date,
    )
    item_ids: dict[str, uuid.UUID] = {}
    async with session_factory() as database:
        for market in markets:
            edition = NewsroomEdition(
                edition_date=edition_date,
                market_code=market,
                auto_publish_at=clock.auto_publish_at(edition_date),
                late_fill_deadline=clock.late_fill_deadline(edition_date),
                status="published" if published else "draft",
                published_at=datetime.now(UTC) if published else None,
            )
            database.add(edition)
            await database.flush()
            item = NewsroomEditionItem(edition_id=edition.id, event_id=event_id, rank=1)
            database.add(item)
            await database.flush()
            item_ids[market] = item.id
        await queue.enqueue(database, queue.ANALYSIS, [event_id])
        await database.commit()
    return event_id, item_ids


def _analysis_answer(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "headline": "聯準會維持利率不變",
        "summary": "聯準會宣布維持利率。市場關注軟體類股。",
        "related_symbols": [
            {"symbol": "^GSPC", "kind": "index", "label": "標普 500"},
            {"symbol": "ACME", "kind": "equity", "label": "不存在"},
        ],
        "why": [{"market": market, "why": f"{market} 的滑鼠理由"} for market in payload["markets"]],
    }


async def test_analysis_without_full_text_needs_body_without_calling_the_model(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    event_id, _ = await _placed_event(newsroom_database, body=False)
    llm = FakeLlm()
    runtime = _runtime(newsroom_database, llm)

    stages = analysis.register(runtime).stages
    assert await _run_stage(runtime, _binding(stages, queue.ANALYSIS)) == ["done"]

    event = await _get(newsroom_database, NewsroomEvent, event_id)
    assert event.analysis_status == "needs_body"
    assert event.analysis_next_attempt_at is None
    assert llm.calls == []


async def test_analysis_writes_shared_fields_and_every_market_why(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    event_id, item_ids = await _placed_event(newsroom_database)
    llm = FakeLlm({AnalysisResult: _analysis_answer})
    runtime = _runtime(newsroom_database, llm)

    stages = analysis.register(runtime).stages
    assert await _run_stage(runtime, _binding(stages, queue.ANALYSIS)) == ["done"]

    event = await _get(newsroom_database, NewsroomEvent, event_id)
    assert event.analysis_status == "ready"
    assert event.headline_zh_hant == "聯準會維持利率不變"
    assert event.headline_zh_hans == "联准会维持利率不变"
    assert event.summary_zh_hans == "联准会宣布维持利率。市场关注软件类股。"
    assert event.related_symbols == [{"symbol": "^GSPC", "kind": "index", "label": "標普 500"}]
    assert event.analysis_model == "deepseek-chat"
    assert event.analyzed_at is not None
    for market, item_id in item_ids.items():
        item = await _get(newsroom_database, NewsroomEditionItem, item_id)
        assert item.why_status == "ready"
        assert item.why_zh_hant == f"{market} 的滑鼠理由"
        assert item.why_zh_hans == f"{market} 的鼠标理由"
    payload = llm.calls[0][1]
    assert payload["markets"] == ["global", "tw_equity"]
    assert payload["articles"][0]["body"].startswith("Fed holds 全文")
    # Nothing to WHY: analysis already wrote every item.
    assert await _run_stage(runtime, _binding(stages, queue.WHY)) == []


async def test_analysis_rewrites_every_unremoved_item_pointing_at_the_event(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    rounds = 0

    def answer(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal rounds
        rounds += 1
        result = _analysis_answer(payload)
        result["why"] = [
            {"market": market, "why": f"{market} 第 {rounds} 次分析 軟體"}
            for market in payload["markets"]
        ]
        return result

    llm = FakeLlm({AnalysisResult: answer})
    runtime = _runtime(newsroom_database, llm)
    binding = _binding(analysis.register(runtime).stages, queue.ANALYSIS)
    target, target_items = await _placed_event(newsroom_database, markets=("global",))
    assert await _run_stage(runtime, binding) == ["done"]
    source, source_items = await _placed_event(newsroom_database, markets=("tw_equity",))

    async with newsroom_database() as database:
        # Admin removes the global placement from the draft.
        await database.execute(
            update(NewsroomEditionItem)
            .where(NewsroomEditionItem.id == target_items["global"])
            .values(removed_at=datetime.now(UTC))
        )
        # Workstream ② merges source into target: articles and items move, and
        # only the target's analysis is re-queued (item why_status untouched).
        await database.execute(
            update(NewsroomArticle)
            .where(NewsroomArticle.event_id == source)
            .values(event_id=target)
        )
        await database.execute(
            update(NewsroomEvent)
            .where(NewsroomEvent.id == source)
            .values(status="merged", merged_into_id=target)
        )
        await database.execute(
            update(NewsroomEditionItem)
            .where(NewsroomEditionItem.id == source_items["tw_equity"])
            .values(event_id=target)
        )
        # A new market placement, already hidden in its published edition.
        edition = NewsroomEdition(
            edition_date=EDITION_DATE,
            market_code="us_equity",
            auto_publish_at=clock.auto_publish_at(EDITION_DATE),
            late_fill_deadline=clock.late_fill_deadline(EDITION_DATE),
            status="published",
            published_at=datetime.now(UTC),
        )
        database.add(edition)
        await database.flush()
        hidden = NewsroomEditionItem(
            edition_id=edition.id, event_id=target, rank=1, hidden_at=datetime.now(UTC)
        )
        database.add(hidden)
        await queue.enqueue(database, queue.ANALYSIS, [target])
        await database.commit()

    assert sorted(await _run_stage(runtime, binding)) == ["done", "done"]

    assert llm.calls[-1][1]["markets"] == ["tw_equity", "us_equity"]
    assert len(llm.calls) == 2
    moved = await _get(newsroom_database, NewsroomEditionItem, source_items["tw_equity"])
    added = await _get(newsroom_database, NewsroomEditionItem, hidden.id)
    removed = await _get(newsroom_database, NewsroomEditionItem, target_items["global"])
    assert (moved.why_status, moved.why_zh_hant) == ("ready", "tw_equity 第 2 次分析 軟體")
    assert (added.why_status, added.why_zh_hans) == ("ready", "us_equity 第 2 次分析 软件")
    assert (removed.why_status, removed.why_zh_hant) == ("ready", "global 第 1 次分析 軟體")
    assert (await _get(newsroom_database, NewsroomEvent, target)).analysis_status == "ready"
    assert (await _get(newsroom_database, NewsroomEvent, source)).analysis_status == "idle"


async def test_analysis_missing_market_why_is_a_retryable_schema_error(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    event_id, _ = await _placed_event(newsroom_database)

    def partial(payload: dict[str, Any]) -> dict[str, Any]:
        answer = _analysis_answer(payload)
        answer["why"] = answer["why"][:1]
        return answer

    runtime = _runtime(newsroom_database, FakeLlm({AnalysisResult: partial}))
    stages = analysis.register(runtime).stages
    assert await _run_stage(runtime, _binding(stages, queue.ANALYSIS)) == ["pending"]
    event = await _get(newsroom_database, NewsroomEvent, event_id)
    assert event.analysis_error_code == "analysis_schema_invalid"
    async with newsroom_database() as database:
        audits = (await database.scalars(select(NewsroomLlmCall))).all()
    assert [(audit.stage, audit.error_code) for audit in audits] == [
        ("analysis", "analysis_schema_invalid")
    ]


async def test_why_stage_waits_for_the_event_analysis(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    event_id, item_ids = await _placed_event(newsroom_database, markets=("global",))
    llm = FakeLlm(
        {
            AnalysisResult: _analysis_answer,
            WhyResult: lambda payload: {"why": f"{payload['market']} 加入後的軟體理由"},
        }
    )
    runtime = _runtime(newsroom_database, llm)
    stages = analysis.register(runtime).stages
    why = _binding(stages, queue.WHY)

    # A second market adds the event while its analysis is still pending.
    async with newsroom_database() as database:
        edition = NewsroomEdition(
            edition_date=EDITION_DATE,
            market_code="us_equity",
            auto_publish_at=clock.auto_publish_at(EDITION_DATE),
            late_fill_deadline=clock.late_fill_deadline(EDITION_DATE),
        )
        database.add(edition)
        await database.flush()
        late = NewsroomEditionItem(edition_id=edition.id, event_id=event_id, rank=1)
        database.add(late)
        await database.commit()
    assert await _run_stage(runtime, why) == []

    # Analysis covers both items, so WHY still has nothing to do.
    assert await _run_stage(runtime, _binding(stages, queue.ANALYSIS)) == ["done"]
    assert await _run_stage(runtime, why) == []

    # An item added after the analysis gets its own WHY call.
    async with newsroom_database() as database:
        edition = NewsroomEdition(
            edition_date=EDITION_DATE,
            market_code="tw_equity",
            auto_publish_at=clock.auto_publish_at(EDITION_DATE),
            late_fill_deadline=clock.late_fill_deadline(EDITION_DATE),
        )
        database.add(edition)
        await database.flush()
        added = NewsroomEditionItem(edition_id=edition.id, event_id=event_id, rank=1)
        database.add(added)
        await database.commit()
    assert await _run_stage(runtime, why) == ["done"]
    item = await _get(newsroom_database, NewsroomEditionItem, added.id)
    assert item.why_status == "ready"
    assert item.why_zh_hant == "tw_equity 加入後的軟體理由"
    assert item.why_zh_hans == "tw_equity 加入后的软件理由"
    assert llm.stages() == ["analysis", "why"]
    assert (await _get(newsroom_database, NewsroomEditionItem, item_ids["global"])).why_zh_hant


# --- Translation ----------------------------------------------------------------------


def _translation(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "headline": "Fed holds rates",
        "summary": "The Fed held rates.",
        "why": {key: f"EN {value}" for key, value in payload["why"].items()},
    }


async def _ready_published_event(
    session_factory: async_sessionmaker[AsyncSession], runtime: Runtime
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    event_id, item_ids = await _placed_event(
        session_factory, markets=("global", "tw_equity"), published=True
    )
    stages = analysis.register(runtime).stages
    assert await _run_stage(runtime, _binding(stages, queue.ANALYSIS)) == ["done"]
    return event_id, item_ids


async def test_translation_covers_visible_items_and_detects_stale_digest(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    llm = FakeLlm({AnalysisResult: _analysis_answer, TranslationResult: _translation})
    runtime = _runtime(newsroom_database, llm)
    event_id, item_ids = await _ready_published_event(newsroom_database, runtime)
    user_id = await _user(newsroom_database)
    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomEditionItem)
            .where(NewsroomEditionItem.id == item_ids["tw_equity"])
            .values(hidden_at=datetime.now(UTC))
        )
        await database.commit()
    translate = translation.register(runtime).stages[0]
    sweep = translation.register(runtime).periodic[0]

    # Published after analysis finished: the sweep queues English.
    await sweep.run()
    assert (await _get(newsroom_database, NewsroomEvent, event_id)).en_status == "pending"
    assert await _run_stage(runtime, translate) == ["done"]

    event = await _get(newsroom_database, NewsroomEvent, event_id)
    assert event.en_status == "ready"
    assert event.headline_en == "Fed holds rates"
    async with newsroom_database() as database:
        assert event.en_source_digest == await translation.current_zh_hant_digest(
            database, event_id
        )
    visible = await _get(newsroom_database, NewsroomEditionItem, item_ids["global"])
    hidden = await _get(newsroom_database, NewsroomEditionItem, item_ids["tw_equity"])
    assert visible.why_en == "EN global 的滑鼠理由"
    assert visible.why_en_status == "ready"
    assert hidden.why_en is None
    assert llm.calls[-1][1]["why"] == {str(item_ids["global"]): "global 的滑鼠理由"}

    # Nothing changed: the sweep leaves it alone and a forced run skips the model.
    await sweep.run()
    assert (await _get(newsroom_database, NewsroomEvent, event_id)).en_status == "ready"
    async with newsroom_database() as database:
        await queue.enqueue(database, queue.TRANSLATE, [event_id])
        await database.commit()
    calls = len(llm.calls)
    assert await _run_stage(runtime, translate) == ["done"]
    assert len(llm.calls) == calls

    # Unhiding changes the visible set; editing the zh-hant changes the text.
    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomEditionItem)
            .where(NewsroomEditionItem.id == item_ids["tw_equity"])
            .values(hidden_at=None)
        )
        await database.commit()
    await sweep.run()
    assert (await _get(newsroom_database, NewsroomEvent, event_id)).en_status == "pending"
    assert await _run_stage(runtime, translate) == ["done"]
    hidden = await _get(newsroom_database, NewsroomEditionItem, item_ids["tw_equity"])
    assert hidden.why_en == "EN tw_equity 的滑鼠理由"

    # A rewritten "why" is translated again; the digest readers rely on stays.
    signed = (await _get(newsroom_database, NewsroomEvent, event_id)).en_source_digest
    async with newsroom_database() as database:
        await publishing.apply_why_edit(database, item_ids["global"], "改寫的理由", user_id=user_id)
        await database.commit()
    rewritten = await _get(newsroom_database, NewsroomEditionItem, item_ids["global"])
    assert rewritten.why_en_status == "idle"
    assert (await _get(newsroom_database, NewsroomEvent, event_id)).en_status == "pending"
    assert await _run_stage(runtime, translate) == ["done"]
    rewritten = await _get(newsroom_database, NewsroomEditionItem, item_ids["global"])
    assert (rewritten.why_en, rewritten.why_en_status) == ("EN 改寫的理由", "ready")
    assert (await _get(newsroom_database, NewsroomEvent, event_id)).en_source_digest == signed

    async with newsroom_database() as database:
        await publishing.apply_event_edit(
            database,
            event_id,
            headline="聯準會按兵不動",
            summary=None,
            related_symbols=None,
            user_id=user_id,
        )
        await database.commit()
    event = await _get(newsroom_database, NewsroomEvent, event_id)
    assert event.en_status == "pending"
    async with newsroom_database() as database:
        assert event.en_source_digest != await translation.current_zh_hant_digest(
            database, event_id
        )


async def test_translation_rejects_a_missing_why(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    def incomplete(payload: dict[str, Any]) -> dict[str, Any]:
        return {**_translation(payload), "why": {}}

    llm = FakeLlm({AnalysisResult: _analysis_answer, TranslationResult: incomplete})
    runtime = _runtime(newsroom_database, llm)
    event_id, _ = await _ready_published_event(newsroom_database, runtime)
    async with newsroom_database() as database:
        await queue.enqueue(database, queue.TRANSLATE, [event_id])
        await database.commit()
    translate = translation.register(runtime).stages[0]
    assert await _run_stage(runtime, translate) == ["pending"]
    event = await _get(newsroom_database, NewsroomEvent, event_id)
    assert event.en_error_code == "translate_schema_invalid"
    assert event.en_source_digest is None


# --- Publishing ------------------------------------------------------------------------


async def test_auto_publish_releases_drafts_at_nine(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    llm = FakeLlm({AnalysisResult: _analysis_answer})
    runtime = _runtime(newsroom_database, llm)
    event_id, item_ids = await _placed_event(newsroom_database, markets=("global",))
    stages = analysis.register(runtime).stages
    await _run_stage(runtime, _binding(stages, queue.ANALYSIS))
    nine = clock.auto_publish_at(EDITION_DATE)

    async with newsroom_database() as database:
        assert await publishing.auto_publish_due(database, now=nine - timedelta(seconds=1)) == 0
        await database.commit()
    assert (await _editions(newsroom_database))["global"].status == "draft"

    async with newsroom_database() as database:
        assert await publishing.auto_publish_due(database, now=nine) == 1
        await database.commit()
    edition = (await _editions(newsroom_database))["global"]
    assert edition.status == "published"
    assert edition.published_at == nine
    assert edition.published_by_user_id is None
    assert (await _get(newsroom_database, NewsroomEvent, event_id)).en_status == "pending"
    async with newsroom_database() as database:
        assert (await database.scalars(select(NewsroomEditLog))).all() == []
        assert await publishing.auto_publish_due(database, now=nine) == 0
    del item_ids


async def test_auto_publish_holds_an_empty_draft_and_notifies_once(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    runtime = _runtime(newsroom_database, FakeLlm())
    user_id = await _user(newsroom_database)
    event_id, item_ids = await _placed_event(newsroom_database, markets=("tw_equity", "us_equity"))
    async with newsroom_database() as database:
        # tw_equity's only item was removed by an admin; global has none at all.
        await database.execute(
            update(NewsroomEditionItem)
            .where(NewsroomEditionItem.id == item_ids["tw_equity"])
            .values(removed_at=datetime.now(UTC))
        )
        database.add(
            NewsroomEdition(
                edition_date=EDITION_DATE,
                market_code="global",
                auto_publish_at=clock.auto_publish_at(EDITION_DATE),
                late_fill_deadline=clock.late_fill_deadline(EDITION_DATE),
            )
        )
        # The outage: triage failed for the window's articles.
        await database.execute(
            update(NewsroomArticle)
            .where(NewsroomArticle.event_id == event_id)
            .values(triage_status="failed", triage_error_code="triage_provider_http_402")
        )
        await database.commit()
    auto_publish = next(
        task
        for task in publishing.register(runtime).periodic
        if task.name == "newsroom_auto_publish"
    )

    # The real 09:00 of 2026-09-30 has passed.
    await auto_publish.run()
    editions = await _editions(newsroom_database)
    assert editions["us_equity"].status == "published"
    assert editions["global"].status == "draft"
    assert editions["tw_equity"].status == "draft"
    assert editions["global"].held_empty_at is not None
    assert editions["tw_equity"].held_empty_at == editions["global"].held_empty_at
    assert editions["us_equity"].held_empty_at is None
    notices = _notices(runtime).sent
    assert [notice.kind for notice in notices] == ["edition_empty_held"]
    assert notices[0].title == "2026-09-30 版次沒有任何項目 暫停自動發布"
    assert notices[0].lines[:2] == (
        "市場 global, tw_equity",
        "收稿 1 篇 初篩失敗 1 篇 (triage_provider_http_402 1)",
    )
    assert notices[0].link == "https://admin.test/admin/newsroom?date=2026-09-30"

    # Still empty a minute later: no second notice.
    await auto_publish.run()
    assert len(_notices(runtime).sent) == 1

    # A re-assembly gives global an item; the next sweep publishes it.
    source = await _source(newsroom_database, "reassembled")
    late_event = await _event(newsroom_database, "Late story", sources=[source], scores={})
    async with newsroom_database() as database:
        database.add(
            NewsroomEditionItem(edition_id=editions["global"].id, event_id=late_event, rank=1)
        )
        await database.commit()
    await auto_publish.run()
    editions = await _editions(newsroom_database)
    assert editions["global"].status == "published"
    assert editions["tw_equity"].status == "draft"
    assert len(_notices(runtime).sent) == 1

    # An admin may still approve the empty edition.
    async with newsroom_database() as database:
        await publishing.publish_edition(database, editions["tw_equity"].id, user_id=user_id)
        await database.commit()
    assert (await _editions(newsroom_database))["tw_equity"].status == "published"


async def test_hold_empty_due_skips_drafts_not_yet_due(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        database.add(
            NewsroomEdition(
                edition_date=EDITION_DATE,
                market_code="global",
                auto_publish_at=clock.auto_publish_at(EDITION_DATE),
                late_fill_deadline=clock.late_fill_deadline(EDITION_DATE),
            )
        )
        await database.commit()
    nine = clock.auto_publish_at(EDITION_DATE)
    async with newsroom_database() as database:
        assert await publishing.hold_empty_due(database, now=nine - timedelta(seconds=1)) == []
        assert await publishing.auto_publish_due(database, now=nine) == 0
        held = await publishing.hold_empty_due(database, now=nine)
        assert [(edition.edition_date, edition.market_code) for edition in held] == [
            (EDITION_DATE, "global")
        ]
        assert await publishing.hold_empty_due(database, now=nine) == []
        assert await publishing.triage_summary(database, EDITION_DATE) == "收稿時段沒有任何文章"
        await database.commit()


async def test_late_fill_close_abandons_unready_items_once_and_notifies(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    llm = FakeLlm({AnalysisResult: _analysis_answer})
    runtime = _runtime(newsroom_database, llm)
    ready_event, ready_items = await _placed_event(
        newsroom_database, markets=("global",), published=True
    )
    await _run_stage(runtime, _binding(analysis.register(runtime).stages, queue.ANALYSIS))
    # Its "why" is still being written; readers never see one, so it is kept.
    async with newsroom_database() as database:
        await database.execute(
            update(NewsroomEditionItem)
            .where(NewsroomEditionItem.id == ready_items["global"])
            .values(why_status="pending", why_zh_hant=None, why_zh_hans=None)
        )
        await database.commit()
    source = await _source(newsroom_database, "late")
    late_event = await _event(newsroom_database, "Late story", sources=[source], scores={})
    edition = (await _editions(newsroom_database))["global"]
    async with newsroom_database() as database:
        late_item = NewsroomEditionItem(edition_id=edition.id, event_id=late_event, rank=2)
        database.add(late_item)
        await database.commit()
    noon = clock.late_fill_deadline(EDITION_DATE)

    async with newsroom_database() as database:
        assert await publishing.close_late_fill(database, now=noon - timedelta(seconds=1)) == []
        await database.commit()

    # The real deadline (2026-09-30 12:00) has passed, so the periodic task closes it.
    late_fill = next(
        task
        for task in publishing.register(runtime).periodic
        if task.name == "newsroom_late_fill_close"
    )
    await late_fill.run()
    abandoned = await _get(newsroom_database, NewsroomEditionItem, late_item.id)
    kept = await _get(newsroom_database, NewsroomEditionItem, ready_items["global"])
    assert abandoned.abandoned_at is not None
    assert kept.abandoned_at is None
    edition = (await _editions(newsroom_database))["global"]
    assert edition.late_fill_closed_at is not None
    notices = _notices(runtime).sent
    assert [notice.kind for notice in notices] == ["late_fill_abandoned"]
    assert notices[0].lines == ("Late story",)

    await late_fill.run()
    assert len(_notices(runtime).sent) == 1
    del ready_event


async def test_publishing_admin_functions_write_edit_log(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    llm = FakeLlm({AnalysisResult: _analysis_answer})
    runtime = _runtime(newsroom_database, llm)
    user_id = await _user(newsroom_database)
    event_id, item_ids = await _placed_event(newsroom_database, markets=("global",))
    await _run_stage(runtime, _binding(analysis.register(runtime).stages, queue.ANALYSIS))
    edition = (await _editions(newsroom_database))["global"]
    source = await _source(newsroom_database, "manual-src")
    candidate = await _event(newsroom_database, "Candidate", sources=[source], scores={})
    other_day = await _event(
        newsroom_database,
        "Other day",
        sources=[source],
        scores={},
        edition_date=EDITION_DATE - timedelta(days=1),
    )

    async with newsroom_database() as database:
        item_id = await publishing.add_event_to_edition(
            database, edition.id, candidate, user_id=user_id
        )
        with pytest.raises(ValueError, match="already in this edition"):
            await publishing.add_event_to_edition(database, edition.id, candidate, user_id=user_id)
        await database.commit()
    async with newsroom_database() as database:
        with pytest.raises(ValueError, match="another edition date"):
            await publishing.add_event_to_edition(database, edition.id, other_day, user_id=user_id)
        with pytest.raises(LookupError):
            await publishing.add_event_to_edition(
                database, edition.id, uuid.uuid4(), user_id=user_id
            )
    item = await _get(newsroom_database, NewsroomEditionItem, item_id)
    assert (item.rank, item.origin, item.added_by_user_id) == (2, "manual", user_id)
    assert (await _get(newsroom_database, NewsroomEvent, candidate)).analysis_status == "pending"

    async with newsroom_database() as database:
        await publishing.apply_event_edit(
            database,
            event_id,
            headline="聯準會按兵不動",
            summary="摘要改寫 軟體。",
            related_symbols=[{"symbol": "^twii", "kind": "index", "label": "加權指數"}],
            user_id=user_id,
        )
        with pytest.raises(ValueError, match="dashboard"):
            await publishing.apply_event_edit(
                database,
                event_id,
                headline=None,
                summary=None,
                related_symbols=[{"symbol": "ACME", "kind": "equity", "label": "x"}],
                user_id=user_id,
            )
        await database.commit()
    event = await _get(newsroom_database, NewsroomEvent, event_id)
    assert event.headline_zh_hans == "联准会按兵不动"
    assert event.summary_zh_hans == "摘要改写 软件。"
    assert event.related_symbols == [{"symbol": "^TWII", "kind": "index", "label": "加權指數"}]
    assert event.edited_by_user_id == user_id
    # Still a draft: English waits for publication.
    assert event.en_status == "idle"

    async with newsroom_database() as database:
        await publishing.apply_why_edit(database, item_ids["global"], "改寫的理由", user_id=user_id)
        await publishing.publish_edition(database, edition.id, user_id=user_id)
        await database.commit()
    why_item = await _get(newsroom_database, NewsroomEditionItem, item_ids["global"])
    assert (why_item.why_zh_hant, why_item.why_zh_hans) == ("改寫的理由", "改写的理由")
    edition = (await _editions(newsroom_database))["global"]
    assert edition.status == "published"
    assert edition.published_by_user_id == user_id
    assert (await _get(newsroom_database, NewsroomEvent, event_id)).en_status == "pending"

    async with newsroom_database() as database:
        await publishing.reanalyze_event(database, event_id, user_id=user_id)
        await database.commit()
    event = await _get(newsroom_database, NewsroomEvent, event_id)
    assert (event.analysis_status, event.analysis_attempts) == ("pending", 0)
    why_item = await _get(newsroom_database, NewsroomEditionItem, item_ids["global"])
    assert why_item.why_status == "pending"

    async with newsroom_database() as database:
        log = (
            await database.execute(
                select(NewsroomEditLog.entity_type, NewsroomEditLog.action).order_by(
                    NewsroomEditLog.created_at, NewsroomEditLog.id
                )
            )
        ).all()
    assert sorted(log) == sorted(
        [
            ("item", "add"),
            ("event", "edit"),
            ("item", "edit_why"),
            ("edition", "approve"),
            ("event", "reanalyze"),
        ]
    )


async def test_stage_error_after_retries_marks_analysis_failed(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    event_id, _ = await _placed_event(newsroom_database)
    llm = FakeLlm({AnalysisResult: lambda payload: RetryableStageError("analysis_timeout")})
    runtime = _runtime(newsroom_database, llm)
    binding = _binding(analysis.register(runtime).stages, queue.ANALYSIS)
    future = datetime.now(UTC) + timedelta(days=1)
    for _ in range(queue.ANALYSIS.max_attempts):
        await _run_stage(runtime, binding, now=future)
        future += timedelta(days=1)
    event = await _get(newsroom_database, NewsroomEvent, event_id)
    assert event.analysis_status == "failed"
    assert event.analysis_error_code == "analysis_timeout"


async def test_assembly_merges_duplicate_candidates_before_rating(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    reuters = await _source(newsroom_database, "reuters")
    cna = await _source(newsroom_database, "cna")
    covered = await _event(
        newsroom_database,
        "payrolls-a",
        sources=[reuters, cna],
        scores={"global": 90, "tw_equity": 0, "us_equity": 80},
    )
    repeat = await _event(
        newsroom_database,
        "payrolls-b",
        sources=[cna],
        scores={"global": 88, "tw_equity": 0, "us_equity": 78},
    )
    other = await _event(
        newsroom_database, "oil", sources=[cna], scores={"global": 70, "tw_equity": 0}
    )
    preview = await _event(
        newsroom_database, "preview", sources=[cna], scores={"global": 60, "us_equity": 50}
    )

    def groups(payload: dict[str, Any]) -> dict[str, Any]:
        ids = {event["working_title"]: event["event_id"] for event in payload["events"]}
        assert set(ids) == {"payrolls-a", "payrolls-b", "oil", "preview"}
        # The model over-groups the preview; an unknown id and a singleton are ignored.
        return {
            "groups": [
                [ids["payrolls-b"], ids["payrolls-a"], ids["preview"], "unknown"],
                [ids["oil"]],
            ]
        }

    llm = FakeLlm(
        {DuplicateGroups: groups, EditorResult: _stars_by_title({"payrolls-a": 5}, default=3)}
    )
    embedder = TitleEmbedder({"payrolls": [1.0, 0.0], "preview": [0.6, 0.8], "oil": [0.0, 1.0]})
    runtime = _runtime(newsroom_database, llm, embedder)

    report = await assembly.assemble_editions(runtime, EDITION_DATE, sleep=_no_sleep)

    assert report.merged_duplicates == 1
    merged = await _get(newsroom_database, NewsroomEvent, repeat)
    assert (merged.status, merged.merged_into_id) == ("merged", covered)
    async with newsroom_database() as database:
        moved = (
            await database.scalars(
                select(NewsroomArticle.event_id).where(NewsroomArticle.event_id == covered)
            )
        ).all()
        edit_log = (await database.scalars(select(NewsroomEditLog))).all()
    assert len(moved) == 3
    assert edit_log == []
    editions = await _editions(newsroom_database)
    global_items = await _items(newsroom_database, editions["global"].id)
    # The preview's title is not near-identical, so it stays its own event.
    assert (await _get(newsroom_database, NewsroomEvent, preview)).status == "open"
    assert [item.event_id for item in global_items] == [covered, other, preview]
    editor_titles = {
        event["working_title"]
        for stage, payload in llm.calls
        if "market" in payload
        for event in payload["events"]
    }
    assert "payrolls-b" not in editor_titles
    assert "已合併 1 個重複事件" in _notices(runtime).sent[0].lines
    # Both proposals are on record, including the one the threshold blocked.
    async with newsroom_database() as database:
        decisions = (await database.scalars(select(NewsroomDedupeDecision))).all()
    assert {
        (row.event_title, row.matched_title, row.outcome, round(row.title_similarity or 0, 2))
        for row in decisions
    } == {
        ("payrolls-b", "payrolls-a", "merged", 1.0),
        ("preview", "payrolls-a", "below_threshold", 0.6),
    }
    assert all(
        (row.scope, row.edition_date, row.matched_event_id, row.threshold)
        == ("window", EDITION_DATE, covered, assembly.DEDUPE_TITLE_SIMILARITY)
        for row in decisions
    )


async def test_story_groups_the_screen_cannot_merge_are_limited_at_selection(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    cna = await _source(newsroom_database, "cna")
    angles = ["tsmc-eps", "tsmc-guidance", "tsmc-capex", "tsmc-texas"]
    ids = {
        title: await _event(
            newsroom_database, title, sources=[cna], scores={"tw_equity": 90 - index}
        )
        for index, title in enumerate(angles)
    }
    ids["memory"] = await _event(
        newsroom_database, "memory", sources=[cna], scores={"tw_equity": 50}
    )

    def groups(payload: dict[str, Any]) -> dict[str, Any]:
        return {"groups": [[str(ids[title]) for title in angles]]}

    rate = _stars_by_title(dict.fromkeys(angles, 5), default=3)

    def stars(payload: dict[str, Any]) -> Any:
        labels = {event["working_title"]: event.get("story_group") for event in payload["events"]}
        assert labels == {**dict.fromkeys(angles, "g1"), "memory": None}
        return rate(payload)

    llm = FakeLlm({DuplicateGroups: groups, EditorResult: stars})
    # The angles' working titles are far apart, so none is merged.
    embedder = TitleEmbedder(
        {
            "tsmc-eps": [1.0, 0.0],
            "tsmc-guidance": [0.6, 0.8],
            "tsmc-capex": [0.5, 0.866],
            "tsmc-texas": [0.0, 1.0],
            "memory": [-1.0, 0.0],
        }
    )
    runtime = _runtime(newsroom_database, llm, embedder)

    report = await assembly.assemble_editions(runtime, EDITION_DATE, sleep=_no_sleep)

    assert report.merged_duplicates == 0
    editions = await _editions(newsroom_database)
    items = await _items(newsroom_database, editions["tw_equity"].id)
    # Two of the group, then the 3-star filler; the other angles are rated but not picked.
    assert [item.event_id for item in items] == [
        ids["tsmc-eps"],
        ids["tsmc-guidance"],
        ids["memory"],
    ]
    async with newsroom_database() as database:
        ratings = (
            await database.scalars(
                select(NewsroomEditorRating).where(NewsroomEditorRating.market_code == "tw_equity")
            )
        ).all()
    assert {(row.event_title, row.stars, row.picked) for row in ratings} == {
        ("tsmc-eps", 5, True),
        ("tsmc-guidance", 5, True),
        ("tsmc-capex", 5, False),
        ("tsmc-texas", 5, False),
        ("memory", 3, True),
    }


async def _embed_articles(
    session_factory: async_sessionmaker[AsyncSession], event_id: uuid.UUID, direction: list[float]
) -> None:
    """Give every article of the event the same embedding, padded to full size."""
    vector = direction + [0.0] * (EMBEDDING_DIMENSIONS - len(direction))
    async with session_factory() as database:
        await database.execute(
            update(NewsroomArticle)
            .where(NewsroomArticle.event_id == event_id)
            .values(embedding=vector)
        )
        await database.commit()


async def test_assembly_merges_angles_of_one_story_whose_articles_are_close(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    reuters = await _source(newsroom_database, "reuters")
    cna = await _source(newsroom_database, "cna")
    scores = {"tw_equity": 90}
    results = await _event(newsroom_database, "tsmc-results", sources=[reuters, cna], scores=scores)
    guidance = await _event(newsroom_database, "tsmc-guidance", sources=[cna], scores=scores)
    preview = await _event(newsroom_database, "tsmc-preview", sources=[cna], scores=scores)
    texas = await _event(newsroom_database, "tsmc-texas", sources=[cna], scores={"tw_equity": 80})
    await _embed_articles(newsroom_database, results, [1.0, 0.0])
    await _embed_articles(newsroom_database, guidance, [0.95, 0.312])
    await _embed_articles(newsroom_database, preview, [0.87, 0.493])
    # tsmc-texas has no embedded article, so only its title could match.
    events = {"tsmc-results": results, "tsmc-guidance": guidance, "tsmc-preview": preview}
    events["tsmc-texas"] = texas

    def groups(payload: dict[str, Any]) -> dict[str, Any]:
        return {"groups": [[str(event_id) for event_id in events.values()]]}

    llm = FakeLlm({DuplicateGroups: groups, EditorResult: _stars_by_title({}, default=4)})
    # Every working title is far from the others.
    embedder = TitleEmbedder(
        {
            "tsmc-results": [1.0, 0.0],
            "tsmc-guidance": [0.0, 1.0],
            "tsmc-preview": [-1.0, 0.0],
            "tsmc-texas": [0.0, -1.0],
        }
    )
    runtime = _runtime(newsroom_database, llm, embedder)

    report = await assembly.assemble_editions(runtime, EDITION_DATE, sleep=_no_sleep)

    assert report.merged_duplicates == 1
    merged = await _get(newsroom_database, NewsroomEvent, guidance)
    assert (merged.status, merged.merged_into_id) == ("merged", results)
    for event_id in (preview, texas):
        assert (await _get(newsroom_database, NewsroomEvent, event_id)).status == "open"
    async with newsroom_database() as database:
        decisions = (await database.scalars(select(NewsroomDedupeDecision))).all()
    assert {
        (
            row.event_title,
            row.outcome,
            None if row.article_similarity is None else round(row.article_similarity, 2),
        )
        for row in decisions
    } == {
        ("tsmc-guidance", "merged", 0.95),
        ("tsmc-preview", "below_threshold", 0.87),
        ("tsmc-texas", "below_threshold", None),
    }
    # The two left open stay a story group: with the target, at most two are picked.
    editions = await _editions(newsroom_database)
    items = await _items(newsroom_database, editions["tw_equity"].id)
    assert [item.event_id for item in items] == [results, preview]


@pytest.mark.parametrize("variant", ["earnings_merge", "p0b", "p0", "baseline"])
async def test_earnings_merge_joins_one_release_s_angles_whatever_their_similarity(
    newsroom_database: async_sessionmaker[AsyncSession], variant: str
) -> None:
    reuters = await _source(newsroom_database, "reuters")
    cna = await _source(newsroom_database, "cna")
    scores = {"tw_equity": 90}
    data = ["data", "data"]
    titles = {
        "台積電第三季獲利優於預期": data,
        "台積電上調全年財測": ["data"],
        "台積電法說會 董事長談AI需求": ["opinion"],
        "台積電發表新製程": ["data"],
        "聯發科第三季獲利優於預期": ["data"],
    }
    events = {
        title: await _event(
            newsroom_database,
            title,
            sources=[reuters, cna][: len(types)],
            scores=scores,
            story_types=types,
        )
        for title, types in titles.items()
    }

    def groups(payload: dict[str, Any]) -> dict[str, Any]:
        return {"groups": [[str(event_id) for event_id in events.values()]]}

    llm = FakeLlm({DuplicateGroups: groups, EditorResult: _stars_by_title({}, default=4)})
    # Every working title is far from the others, and no article is embedded.
    directions = ([1.0, 0.0], [0.0, 1.0], [-1.0, 0.0], [0.0, -1.0], [0.7, 0.7])
    embedder = TitleEmbedder(dict(zip(titles, directions, strict=True)))
    runtime = _with_variant(_runtime(newsroom_database, llm, embedder), variant)

    report = await assembly.assemble_editions(runtime, EDITION_DATE, sleep=_no_sleep)

    async with newsroom_database() as database:
        decisions = (await database.scalars(select(NewsroomDedupeDecision))).all()
    outcomes = {row.event_title: (row.outcome, row.threshold) for row in decisions}
    target = events["台積電第三季獲利優於預期"]
    guidance = await _get(newsroom_database, NewsroomEvent, events["台積電上調全年財測"])
    if variant == "earnings_merge":
        assert report.merged_duplicates == 1
        assert (guidance.status, guidance.merged_into_id) == ("merged", target)
        assert outcomes.pop("台積電上調全年財測") == ("merged", assembly.EARNINGS_MERGE_THRESHOLD)
    else:
        assert report.merged_duplicates == 0
        assert guidance.status == "open"
        assert outcomes.pop("台積電上調全年財測") == (
            "below_threshold",
            assembly.DEDUPE_TITLE_SIMILARITY,
        )
    # The chair's remarks (opinion), another story of the company and another
    # company's results stay apart under every variant.
    assert outcomes == dict.fromkeys(
        ("台積電法說會 董事長談AI需求", "台積電發表新製程", "聯發科第三季獲利優於預期"),
        ("below_threshold", assembly.DEDUPE_TITLE_SIMILARITY),
    )


async def test_price_action_reports_neither_qualify_nor_score(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    reuters = await _source(newsroom_database, "reuters")
    cna = await _source(newsroom_database, "cna")
    udn = await _source(newsroom_database, "udn")
    await _event(
        newsroom_database,
        "台股大漲創新高",
        sources=[reuters, cna, udn],
        scores={"tw_equity": 40},
        story_types=["price_action"] * 3,
    )
    # One report of the decision itself, two of the market's reaction to it.
    decision = await _event(
        newsroom_database,
        "央行升息半碼",
        sources=[reuters, cna, udn],
        scores={"tw_equity": 90},
        story_types=["official", "price_action", "price_action"],
    )
    llm = FakeLlm({EditorResult: _stars_by_title({}, default=4)})
    runtime = _runtime(newsroom_database, llm)

    await assembly.assemble_editions(runtime, EDITION_DATE, sleep=_no_sleep)

    [(_, payload)] = [call for call in llm.calls if call[1]["market"] == "tw_equity"]
    assert payload["events"] == [
        {
            "event_id": str(decision),
            "working_title": "央行升息半碼",
            "report_count": 1,
            "source_count": 1,
            # The p0 variant's extended input; these articles carry no triage signals.
            "max_impact": None,
            "max_escalation": 0,
            "main_story_type": "official",
            "headlines": [{"title": "央行升息半碼 report 0", "source": "Reuters"}],
        }
    ]
    editions = await _editions(newsroom_database)
    [item] = await _items(newsroom_database, editions["tw_equity"].id)
    # Scored on the decision alone: no bonus for the two reaction pieces.
    assert (item.event_id, item.editor_score) == (decision, 90.0)


def _with_variant(runtime: Runtime, variant: str) -> Runtime:
    return replace(
        runtime,
        settings=runtime.settings.model_copy(update={"newsroom_selection_variant": variant}),
    )


_HEADLINES = [
    {"title": "航道遇襲 report 0", "source": "Reuters"},
    {"title": "航道遇襲 report 1", "source": "Cna"},
]


async def _extended_input_event(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    reuters = await _source(newsroom_database, "reuters")
    cna = await _source(newsroom_database, "cna")
    udn = await _source(newsroom_database, "udn")
    event_id = await _event(
        newsroom_database,
        "航道遇襲",
        sources=[reuters, cna, udn],
        scores={"global": 90},
        story_types=["catalyst", "catalyst", "price_action"],
    )
    signals = {
        "航道遇襲 report 0": ({"impact": 5, "new_facts": True, "stale": False}, None),
        "航道遇襲 report 1": (
            {"impact": 4, "new_facts": True, "escalation": 2, "stale": False},
            "油輪改道。保費上升。運價走高。",
        ),
        # A reaction piece: neither its impact nor its escalation counts.
        "航道遇襲 report 2": ({"impact": 5, "escalation": 2}, "油價大漲。"),
    }
    async with newsroom_database() as database:
        for title, (triage_signals, summary) in signals.items():
            await database.execute(
                update(NewsroomArticle)
                .where(NewsroomArticle.title == title)
                .values(triage_signals=triage_signals, feed_summary=summary)
            )
        await database.commit()
    return event_id


async def test_p0_editor_input_reads_stored_signals_and_summaries(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    event_id = await _extended_input_event(newsroom_database)
    llm = FakeLlm({EditorResult: _stars_by_title({}, default=4)})

    await assembly.assemble_editions(
        _with_variant(_runtime(newsroom_database, llm), "p0"), EDITION_DATE, sleep=_no_sleep
    )

    [(_, payload)] = [call for call in llm.calls if call[1]["market"] == "global"]
    # Both reports were first seen together, so their headline order is arbitrary;
    # either way the first with a summary is report 1.
    assert sorted(payload["events"][0].pop("headlines"), key=lambda line: line["title"]) == (
        _HEADLINES
    )
    assert payload["events"] == [
        {
            "event_id": str(event_id),
            "working_title": "航道遇襲",
            "report_count": 2,
            "source_count": 2,
            "max_impact": 5,
            "max_escalation": 2,
            "main_story_type": "catalyst",
            "lead_summary": "油輪改道。保費上升。",
        }
    ]


async def test_baseline_editor_input_keeps_the_eb6ad34_fields(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    event_id = await _extended_input_event(newsroom_database)
    llm = FakeLlm({EditorResult: _stars_by_title({}, default=4)})

    await assembly.assemble_editions(
        _with_variant(_runtime(newsroom_database, llm), "baseline"), EDITION_DATE, sleep=_no_sleep
    )

    [(_, payload)] = [call for call in llm.calls if call[1]["market"] == "global"]
    assert sorted(payload["events"][0].pop("headlines"), key=lambda line: line["title"]) == (
        _HEADLINES
    )
    assert payload["events"] == [
        {
            "event_id": str(event_id),
            "working_title": "航道遇襲",
            "report_count": 2,
            "source_count": 2,
        }
    ]


async def _editor_audit(newsroom_database: async_sessionmaker[AsyncSession]) -> list[str | None]:
    async with newsroom_database() as database:
        return list(
            await database.scalars(
                select(NewsroomLlmCall.prompt_version).where(NewsroomLlmCall.stage == "editor")
            )
        )


async def _stored_ratings(
    newsroom_database: async_sessionmaker[AsyncSession], market: str
) -> list[NewsroomEditorRating]:
    async with newsroom_database() as database:
        return list(
            await database.scalars(
                select(NewsroomEditorRating).where(NewsroomEditorRating.market_code == market)
            )
        )


async def test_p0_editor_uses_prompt_v4_and_keeps_its_reasons(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    await _extended_input_event(newsroom_database)
    llm = FakeLlm(
        {
            EditorResult: lambda payload: {
                "ratings": [
                    {"event_id": event["event_id"], "stars": 2, "reason": " 與本版關聯間接 "}
                    for event in payload["events"]
                ]
            }
        }
    )

    await assembly.assemble_editions(
        _with_variant(_runtime(newsroom_database, llm), "p0"), EDITION_DATE, sleep=_no_sleep
    )

    assert {system for stage, system in llm.systems if stage == "editor"} == {
        translation.load_prompt("editor")
    }
    assert set(await _editor_audit(newsroom_database)) == {"newsroom.editor.v4"}
    [rating] = await _stored_ratings(newsroom_database, "global")
    assert (rating.stars, rating.reason, rating.prompt_version) == (
        2,
        "與本版關聯間接",
        "newsroom.editor.v4",
    )


async def test_a_missing_reason_is_logged_but_never_fails_the_editor(
    newsroom_database: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    await _extended_input_event(newsroom_database)
    # Two stars against a top impact of 5, without the reason the prompt asks for.
    llm = FakeLlm({EditorResult: _stars_by_title({}, default=2)})

    with caplog.at_level("WARNING", logger=assembly.logger.name):
        await assembly.assemble_editions(
            _with_variant(_runtime(newsroom_database, llm), "p0"), EDITION_DATE, sleep=_no_sleep
        )

    # One editor call per market with candidates: no retry, no fallback.
    assert llm.stages().count("editor") == 1
    editions = await _editions(newsroom_database)
    assert editions["global"].selection_mode == "editor"
    [rating] = await _stored_ratings(newsroom_database, "global")
    assert (rating.stars, rating.reason) == (2, None)
    [record] = [row for row in caplog.records if row.message == "newsroom.editor_reasons_missing"]
    assert (record.__dict__["market"], record.__dict__["count"]) == ("global", 1)


async def test_baseline_editor_uses_prompt_v3(
    newsroom_database: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    await _extended_input_event(newsroom_database)
    llm = FakeLlm({EditorResult: _stars_by_title({}, default=2)})

    with caplog.at_level("WARNING", logger=assembly.logger.name):
        await assembly.assemble_editions(
            _with_variant(_runtime(newsroom_database, llm), "baseline"),
            EDITION_DATE,
            sleep=_no_sleep,
        )

    assert {system for stage, system in llm.systems if stage == "editor"} == {
        translation.load_prompt("editor_v3")
    }
    assert set(await _editor_audit(newsroom_database)) == {"newsroom.editor.v3"}
    [rating] = await _stored_ratings(newsroom_database, "global")
    assert (rating.reason, rating.prompt_version) == (None, "newsroom.editor.v3")
    # The v3 prompt asks for no reason, so none is reported missing.
    assert not [row for row in caplog.records if row.message == "newsroom.editor_reasons_missing"]


async def test_feeds_of_one_outlet_count_as_one_source(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    udn_a = await _source(newsroom_database, "udn-money-5590", name="經濟日報")
    udn_b = await _source(newsroom_database, "udn-money-5591", name="經濟日報")
    cna = await _source(newsroom_database, "cna")
    await _event(newsroom_database, "one-outlet", sources=[udn_a, udn_b], scores={"global": 60})
    await _event(newsroom_database, "two-outlets", sources=[udn_a, cna], scores={"global": 60})
    llm = FakeLlm({EditorResult: _stars_by_title({}, default=3)})

    await assembly.assemble_editions(
        _runtime(newsroom_database, llm), EDITION_DATE, sleep=_no_sleep
    )

    [(_, payload)] = [call for call in llm.calls if call[1]["market"] == "global"]
    counts = {event["working_title"]: event["source_count"] for event in payload["events"]}
    assert counts == {"two-outlets": 2, "one-outlet": 1}
    editions = await _editions(newsroom_database)
    scores = sorted(
        item.editor_score or 0.0 for item in await _items(newsroom_database, editions["global"].id)
    )
    # Only the second outlet earns the bonus (p0: 6 x log2(2)).
    assert scores == [60.0, 66.0]


async def test_equal_scores_rank_by_corroboration_then_age(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    wire = await _source(newsroom_database, "wire", trust_tier=3)
    cna = await _source(newsroom_database, "cna")
    udn = await _source(newsroom_database, "udn")
    # All four score 60 ("broad" by its second source's bonus). The tie goes to
    # the story with more sources, then the more trusted one, then the earlier.
    await _event(newsroom_database, "late", sources=[cna], scores={"global": 60}, seen_minutes=9)
    await _event(newsroom_database, "early", sources=[udn], scores={"global": 60}, seen_minutes=1)
    await _event(
        newsroom_database, "trusted", sources=[wire], scores={"global": 60}, seen_minutes=5
    )
    await _event(newsroom_database, "broad", sources=[cna, udn], scores={"global": 55})
    llm = FakeLlm({EditorResult: _stars_by_title({}, default=3)})

    await assembly.assemble_editions(
        _runtime(newsroom_database, llm), EDITION_DATE, sleep=_no_sleep
    )

    [(_, payload)] = [call for call in llm.calls if call[1]["market"] == "global"]
    assert [event["working_title"] for event in payload["events"]] == [
        "broad",
        "trusted",
        "early",
        "late",
    ]
    editions = await _editions(newsroom_database)
    async with newsroom_database() as database:
        event_titles = {
            event.id: event.working_title
            for event in (await database.scalars(select(NewsroomEvent))).all()
        }
    # Equal stars and score keep the candidate order instead of the event id's.
    assert [
        event_titles[item.event_id]
        for item in await _items(newsroom_database, editions["global"].id)
    ] == ["broad", "trusted", "early", "late"]


async def _published_yesterday(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
    title: str,
    *,
    days_ago: int = 1,
    hidden: bool = False,
) -> uuid.UUID:
    """An event a previous edition published, with its reader-facing headline."""
    published_on = EDITION_DATE - timedelta(days=days_ago)
    event_id = await _event(
        session_factory,
        title,
        sources=[source_id],
        scores={"global": 80},
        edition_date=published_on,
    )
    moment = datetime(2026, 9, 29, 1, tzinfo=UTC)
    async with session_factory() as database:
        await database.execute(
            update(NewsroomEvent)
            .where(NewsroomEvent.id == event_id)
            .values(headline_zh_hant=f"{title} 上架標題", summary_zh_hant=f"{title} 摘要")
        )
        edition = await database.scalar(
            select(NewsroomEdition).where(
                NewsroomEdition.edition_date == published_on,
                NewsroomEdition.market_code == "global",
            )
        )
        if edition is None:
            edition = NewsroomEdition(
                edition_date=published_on,
                market_code="global",
                status="published",
                published_at=moment,
                auto_publish_at=clock.auto_publish_at(published_on),
                late_fill_deadline=clock.late_fill_deadline(published_on),
            )
            database.add(edition)
            await database.flush()
        placed = len(
            (
                await database.scalars(
                    select(NewsroomEditionItem.id).where(
                        NewsroomEditionItem.edition_id == edition.id
                    )
                )
            ).all()
        )
        database.add(
            NewsroomEditionItem(
                edition_id=edition.id,
                event_id=event_id,
                rank=placed + 1,
                stars=5,
                hidden_at=moment if hidden else None,
            )
        )
        await database.commit()
    return event_id


async def test_assembly_drops_repeats_of_published_stories_and_flags_follow_ups(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    cna = await _source(newsroom_database, "cna")
    revenue = await _published_yesterday(newsroom_database, cna, "revenue")
    talks = await _published_yesterday(newsroom_database, cna, "talks", days_ago=2)
    # Out of reach: older than the lookback, and one readers never saw.
    await _published_yesterday(newsroom_database, cna, "ancient", days_ago=3)
    await _published_yesterday(newsroom_database, cna, "hidden", hidden=True)
    repeat = await _event(newsroom_database, "revenue-again", sources=[cna], scores={"global": 95})
    loose = await _event(newsroom_database, "revenue-outlook", sources=[cna], scores={"global": 90})
    follow_up = await _event(newsroom_database, "talks-deal", sources=[cna], scores={"global": 85})
    fresh = await _event(newsroom_database, "fresh", sources=[cna], scores={"global": 80})

    def matches(payload: dict[str, Any]) -> dict[str, Any]:
        assert {item["id"] for item in payload["published"]} == {str(revenue), str(talks)}
        assert payload["published"][0] == {
            "id": str(revenue),
            "edition_date": "2026-09-29",
            "headline": "revenue 上架標題",
            "summary": "revenue 摘要",
        }
        ids = {event["working_title"]: event["event_id"] for event in payload["candidates"]}
        assert set(ids) == {"revenue-again", "revenue-outlook", "talks-deal", "fresh"}
        return {
            "matches": [
                {
                    "event_id": ids["revenue-again"],
                    "published_id": str(revenue),
                    "new_facts": False,
                },
                # The model sees nothing new, but the titles differ: not dropped by code.
                {
                    "event_id": ids["revenue-outlook"],
                    "published_id": str(revenue),
                    "new_facts": False,
                },
                {"event_id": ids["talks-deal"], "published_id": str(talks), "new_facts": True},
                # A second match for one candidate and unknown ids are ignored.
                {"event_id": ids["talks-deal"], "published_id": str(revenue), "new_facts": False},
                {"event_id": "unknown", "published_id": str(revenue), "new_facts": False},
                {"event_id": ids["fresh"], "published_id": "unknown", "new_facts": False},
            ]
        }

    llm = FakeLlm({PublishedMatches: matches, EditorResult: _stars_by_title({}, default=4)})
    embedder = TitleEmbedder(
        {"revenue-outlook": [0.6, 0.8], "revenue": [1.0, 0.0], "talks": [0.0, 1.0]}
    )
    runtime = _runtime(newsroom_database, llm, embedder)

    report = await assembly.assemble_editions(runtime, EDITION_DATE, sleep=_no_sleep)

    assert report.excluded_repeats == 1
    [(_, payload)] = [call for call in llm.calls if call[1].get("market") == "global"]
    events = {event["working_title"]: event for event in payload["events"]}
    assert set(events) == {"revenue-outlook", "talks-deal", "fresh"}
    assert events["talks-deal"]["earlier_coverage"] == {
        "edition_date": "2026-09-28",
        "headline": "talks 上架標題",
    }
    assert events["revenue-outlook"]["earlier_coverage"]["headline"] == "revenue 上架標題"
    assert "earlier_coverage" not in events["fresh"]
    editions = await _editions(newsroom_database)
    assert {item.event_id for item in await _items(newsroom_database, editions["global"].id)} == {
        loose,
        follow_up,
        fresh,
    }
    # The repeat stays an open event: an admin can still add it by hand.
    assert (await _get(newsroom_database, NewsroomEvent, repeat)).status == "open"
    async with newsroom_database() as database:
        decisions = (await database.scalars(select(NewsroomDedupeDecision))).all()
    assert {
        (row.event_title, row.matched_title, row.new_facts, row.outcome) for row in decisions
    } == {
        ("revenue-again", "revenue", False, "excluded"),
        ("revenue-outlook", "revenue", False, "follow_up"),
        ("talks-deal", "talks", True, "follow_up"),
    }
    assert all(row.scope == "published" for row in decisions)
    assert "已排除 1 個前版已上架且無新事實的事件" in _notices(runtime).sent[0].lines


def _direction(degrees: float) -> list[float]:
    radians = math.radians(degrees)
    return [math.cos(radians), math.sin(radians)] + [0.0] * (EMBEDDING_DIMENSIONS - 2)


async def _point(
    session_factory: async_sessionmaker[AsyncSession], event_id: uuid.UUID, degrees: float
) -> None:
    """Embed every article of ``event_id`` along one direction of the first plane."""
    async with session_factory() as database:
        await database.execute(
            update(NewsroomArticle)
            .where(NewsroomArticle.event_id == event_id)
            .values(embedding=_direction(degrees))
        )
        await database.commit()


async def test_nearest_published_ranks_by_mean_article_embedding(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    cna = await _source(newsroom_database, "cna")
    near = await _published_yesterday(newsroom_database, cna, "near", days_ago=3)
    middle = await _published_yesterday(newsroom_database, cna, "middle", days_ago=5)
    far = await _published_yesterday(newsroom_database, cna, "far", days_ago=10)
    unembedded = await _published_yesterday(newsroom_database, cna, "unembedded", days_ago=4)
    candidate = await _event(newsroom_database, "candidate", sources=[cna], scores={"global": 90})
    for event_id, degrees in ((near, 10), (middle, 40), (far, 80), (candidate, 0)):
        await _point(newsroom_database, event_id, degrees)
    published = [near, middle, far, unembedded]

    async with newsroom_database() as database:
        assert await assembly.nearest_published(database, [candidate], published, limit=2) == {
            candidate: [near, middle]
        }
        assert await assembly.nearest_published(database, [candidate], published, limit=5) == {
            candidate: [near, middle, far]
        }
        assert await assembly.nearest_published(database, [candidate], [], limit=5) == {}


async def test_repeat_screen_shows_each_candidates_nearest_published_stories(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    cna = await _source(newsroom_database, "cna")
    stories = {
        name: await _published_yesterday(newsroom_database, cna, name, days_ago=days_ago)
        for name, days_ago in (
            ("jobs", 9),
            ("rates", 1),
            ("oil", 2),
            ("chips", 6),
            ("ancient", 15),
        )
    }
    hidden = await _published_yesterday(newsroom_database, cna, "hidden", days_ago=3, hidden=True)
    jobs_again = await _event(newsroom_database, "jobs-again", sources=[cna], scores={"global": 95})
    oil_again = await _event(newsroom_database, "oil-again", sources=[cna], scores={"global": 90})
    angles = {"jobs": 0, "rates": 30, "oil": 90, "chips": 60, "ancient": 1}
    for name, degrees in angles.items():
        await _point(newsroom_database, stories[name], degrees)
    await _point(newsroom_database, hidden, 0)
    await _point(newsroom_database, jobs_again, 0)
    await _point(newsroom_database, oil_again, 89)
    candidates = [jobs_again, oil_again]

    async with newsroom_database() as database:
        nearest_one = replace(OLDNEWS, repeat_nearest_limit=1)
        shown = await assembly.published_for_screen(
            database, EDITION_DATE, candidates, variant=nearest_one
        )
        # 14 days back: ``ancient`` is out of reach and ``hidden`` never reached readers.
        assert set(shown) == {stories["jobs"], stories["oil"]}
        shown = await assembly.published_for_screen(
            database, EDITION_DATE, candidates, variant=replace(OLDNEWS, repeat_nearest_limit=2)
        )
        # A story near both candidates is shown once.
        assert set(shown) == {stories["jobs"], stories["rates"], stories["oil"], stories["chips"]}
        # Without a limit the screen shows the whole lookback, as baseline does.
        assert set(
            await assembly.published_for_screen(
                database,
                EDITION_DATE,
                candidates,
                variant=replace(OLDNEWS, repeat_nearest_limit=None),
            )
        ) == {stories[name] for name in ("jobs", "rates", "oil", "chips")}
        assert set(
            await assembly.published_for_screen(
                database, EDITION_DATE, candidates, variant=BASELINE
            )
        ) == {stories["rates"], stories["oil"]}


async def test_oldnews_assembly_drops_a_repeat_of_a_story_published_days_ago(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    cna = await _source(newsroom_database, "cna")
    jobs = await _published_yesterday(newsroom_database, cna, "jobs", days_ago=9)
    await _point(newsroom_database, jobs, 0)
    for index in range(6):
        other = await _published_yesterday(newsroom_database, cna, f"other{index}", days_ago=1)
        await _point(newsroom_database, other, 30 + 10 * index)
    recap = await _event(newsroom_database, "jobs-recap", sources=[cna], scores={"global": 95})
    await _point(newsroom_database, recap, 2)
    shown: list[set[str]] = []

    def matches(payload: dict[str, Any]) -> dict[str, Any]:
        shown.append({item["headline"] for item in payload["published"]})
        [candidate] = payload["candidates"]
        return {
            "matches": [
                {"event_id": candidate["event_id"], "published_id": str(jobs), "new_facts": False}
            ]
        }

    llm = FakeLlm({PublishedMatches: matches, EditorResult: _stars_by_title({}, default=4)})
    embedder = TitleEmbedder({"jobs": [1.0, 0.0]})
    runtime = _runtime(newsroom_database, llm, embedder, variant="oldnews")

    report = await assembly.assemble_editions(runtime, EDITION_DATE, sleep=_no_sleep)

    # Five nearest of the seven published stories, the nine-day-old one among them.
    assert shown == [
        {"jobs 上架標題", *(f"other{index} 上架標題" for index in range(4))},
    ]
    assert report.excluded_repeats == 1
    async with newsroom_database() as database:
        [decision] = (await database.scalars(select(NewsroomDedupeDecision))).all()
    assert (decision.scope, decision.outcome, decision.matched_title) == (
        "published",
        "excluded",
        "jobs",
    )


async def test_repeat_screen_failure_never_blocks_assembly(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    cna = await _source(newsroom_database, "cna")
    await _published_yesterday(newsroom_database, cna, "revenue")
    await _event(newsroom_database, "revenue-again", sources=[cna], scores={"global": 95})
    llm = FakeLlm(
        {
            PublishedMatches: lambda payload: RetryableStageError("editor_provider_timeout"),
            EditorResult: _stars_by_title({}, default=4),
        }
    )

    report = await assembly.assemble_editions(
        _runtime(newsroom_database, llm), EDITION_DATE, sleep=_no_sleep
    )

    assert report.excluded_repeats == 0
    editions = await _editions(newsroom_database)
    assert len(await _items(newsroom_database, editions["global"].id)) == 1
