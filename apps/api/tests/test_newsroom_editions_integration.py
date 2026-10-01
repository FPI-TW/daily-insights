import os
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
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
    EditorResult,
    TranslationResult,
    WhyResult,
)
from daily_insights_api.modules.newsroom.models import (
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
    RetryableStageError,
    StageSpec,
    claim,
    run_claimed,
)
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
        self.calls.append((audit.stage, payload))
        response = self.responders[result_type](payload)
        if isinstance(response, Exception):
            raise response
        database.add(
            NewsroomLlmCall(
                stage=audit.stage, subject_id=audit.subject_id, model=model, latency_ms=1
            )
        )
        return result_type.model_validate(response)

    def stages(self) -> list[str]:
        return [stage for stage, _ in self.calls]


class NoEmbedder:
    async def embed(self, *args: Any, **kwargs: Any) -> list[list[float]]:
        raise AssertionError("not used")


def _runtime(session_factory: async_sessionmaker[AsyncSession], llm: FakeLlm) -> Runtime:
    return Runtime(
        settings=Settings(newsroom_enabled=True, newsroom_admin_base_url="https://admin.test/"),
        session_factory=session_factory,
        llm=llm,
        embedder=NoEmbedder(),
        notifier=LogNotifier(),
    )


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
) -> uuid.UUID:
    async with session_factory() as database:
        source = NewsroomSource(
            key=key,
            name=key.title(),
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
) -> uuid.UUID:
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
    llm = FakeLlm({EditorResult: _stars_by_title({"global-0": 5, "global-7": 5, "global-3": 4})})
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
    # Two 5-star and one 4-star leave room for two 1-star fillers by score.
    assert [(event_titles[item.event_id], item.stars) for item in items] == [
        ("global-0", 5),
        ("global-7", 5),
        ("global-3", 4),
        ("global-6", 1),
        ("global-5", 1),
    ]
    assert [item.rank for item in items] == [1, 2, 3, 4, 5]
    # Weighted score 50 x 1.5 plus one extra source.
    assert items[0].editor_score == 80.0
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
    llm.responders[EditorResult] = _stars_by_title({"a": 5, "b": 5, "f": 4})
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
    # Model picks first (a, b at 5 stars, f at 4, then 1-star fillers c and d),
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


async def test_late_fill_close_abandons_unready_items_once_and_notifies(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    llm = FakeLlm({AnalysisResult: _analysis_answer})
    runtime = _runtime(newsroom_database, llm)
    ready_event, ready_items = await _placed_event(
        newsroom_database, markets=("global",), published=True
    )
    await _run_stage(runtime, _binding(analysis.register(runtime).stages, queue.ANALYSIS))
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
