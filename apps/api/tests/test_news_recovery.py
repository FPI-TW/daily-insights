import asyncio
import uuid
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_news_integration import news_database as news_database

from daily_insights_api.modules.news import extraction, feeds
from daily_insights_api.modules.news.contracts import Candidate, LocalizedSummary
from daily_insights_api.modules.news.failures import NewsOperationError
from daily_insights_api.modules.news.llm import DeepSeekClient, ModelCall, ModelCallError
from daily_insights_api.modules.news.models import NewsCheckpoint, NewsDependencyState
from daily_insights_api.modules.news.recovery import (
    NewsExecution,
    automatic_window,
    cleanup_checkpoints,
    fingerprint,
    model_step,
    news_execution,
    workflow_results,
    workflow_scope,
)
from daily_insights_api.scripts.run_daily_news_scheduler import reconcile_news


@pytest.mark.integration
async def test_shared_cooldown_survives_new_execution_and_honors_retry_after(
    news_database: async_sessionmaker[AsyncSession],
) -> None:
    current = datetime(2026, 9, 11, 1, tzinfo=UTC)
    due = current + timedelta(minutes=45)
    calls = 0

    async def limited() -> ModelCall:
        nonlocal calls
        calls += 1
        raise ModelCallError(
            "secret",
            error_code="provider_http_429",
            retry_after=due,
            input_digest="a" * 64,
            latency_ms=1,
        )

    for market in ("global", "us_equity", "tw_equity"):
        execution = NewsExecution(
            news_database,
            uuid.uuid4(),
            uuid.uuid4(),
            current.date(),
            automatic=True,
            clock=lambda: current,
        )
        with news_execution(execution), pytest.raises(NewsOperationError):
            async with workflow_scope(news_database, execution.edition_date, market):
                await model_step(fingerprint(market), "selection", limited, lambda _: None)
        progress = (await workflow_results(news_database, execution.run_id))[market]
        assert progress["next_retry_at"] == due.isoformat()
    assert calls == 1
    async with news_database() as database:
        dependency = await database.get(NewsDependencyState, "provider:news")
        assert dependency is not None and dependency.failures_count == 1
        assert dependency.available_at == due


@pytest.mark.integration
async def test_probe_failure_does_not_immediately_make_a_correction_call(
    news_database: async_sessionmaker[AsyncSession],
) -> None:
    execution = NewsExecution(news_database, uuid.uuid4(), uuid.uuid4(), date(2026, 9, 11))
    async with news_database.begin() as database:
        database.add(
            NewsDependencyState(
                scope="provider:news", state="probing", probe_run_id=execution.run_id
            )
        )
    calls = 0

    async def invalid() -> ModelCall:
        nonlocal calls
        calls += 1
        raise ModelCallError(
            "private", error_code="summary_invalid_json", input_digest="a" * 64, latency_ms=1
        )

    with news_execution(execution), pytest.raises(NewsOperationError):
        async with workflow_scope(news_database, execution.edition_date, "global"):
            await model_step(fingerprint("probe"), "summary", invalid, lambda _: None)
    assert calls == 1
    async with news_database() as database:
        dependency = await database.get(NewsDependencyState, "provider:news")
        assert dependency is not None and dependency.state == "attention"
        assert (
            dependency.failure is not None and dependency.failure["code"] == "summary_invalid_json"
        )
        assert dependency.probe_run_id is None


@pytest.mark.integration
async def test_checkpoint_cleanup_leaves_workflow_audit_and_edition_history(
    news_database: async_sessionmaker[AsyncSession],
) -> None:
    from daily_insights_api.modules.news.models import NewsWorkflow

    current = datetime(2026, 9, 11, 1, tzinfo=UTC)
    execution = NewsExecution(
        news_database, uuid.uuid4(), uuid.uuid4(), current.date(), clock=lambda: current
    )
    with news_execution(execution):
        async with workflow_scope(news_database, execution.edition_date, "global") as workflow:
            await workflow.checkpoint(fingerprint("metadata"), "feed")
    await cleanup_checkpoints(news_database, current + timedelta(hours=47))
    async with news_database() as database:
        assert await database.scalar(select(NewsCheckpoint)) is not None
    await cleanup_checkpoints(news_database, current + timedelta(hours=48))
    async with news_database() as database:
        assert await database.scalar(select(NewsCheckpoint)) is None
        assert await database.scalar(select(NewsWorkflow)) is not None


def test_window_excludes_noon_and_previous_day() -> None:
    today = date(2026, 9, 11)
    assert automatic_window(datetime(2026, 9, 11, 1, tzinfo=UTC), today)
    assert not automatic_window(datetime(2026, 9, 11, 4, tzinfo=UTC), today)
    assert not automatic_window(datetime(2026, 9, 11, 0, tzinfo=UTC), today - timedelta(days=1))


@pytest.mark.integration
async def test_resume_reuses_validated_locale_but_invalidates_changed_content(
    news_database: async_sessionmaker[AsyncSession],
) -> None:
    root = uuid.uuid4()
    calls = 0

    async def summarize() -> ModelCall:
        nonlocal calls
        calls += 1
        return ModelCall(
            LocalizedSummary(headline="標題", summary="已驗證摘要"), None, 1, 1, 1, "a" * 64
        )

    for content in ["original", "original", "changed"]:
        execution = NewsExecution(news_database, root, uuid.uuid4(), date(2026, 9, 11))
        with news_execution(execution):
            async with workflow_scope(news_database, execution.edition_date, "us_equity"):
                result = await model_step(
                    fingerprint([content, "en", "model", "prompt"]),
                    "summary",
                    summarize,
                    lambda _: None,
                )
                assert isinstance(result.value, LocalizedSummary)
                assert result.value.summary == "已驗證摘要"
    assert calls == 2


async def test_scheduler_catches_up_at_nine_and_stops_at_noon() -> None:
    current = datetime(2026, 9, 11, 1, tzinfo=UTC)
    called: list[date] = []
    ticks = 0

    async def run(edition: date) -> str:
        called.append(edition)
        return "already_recorded"

    async def advance(_: float) -> None:
        nonlocal current, ticks
        ticks += 1
        current = current.replace(hour=4)
        if ticks == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await reconcile_news(run, now=lambda: current, sleep=advance)
    assert called == [date(2026, 9, 11)]


@pytest.mark.integration
async def test_feed_failure_is_isolated_and_resume_only_fetches_failed_feed(
    news_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = datetime(2026, 9, 11, 1, tzinfo=UTC)
    root = uuid.uuid4()
    calls: Counter[str] = Counter()

    def clock() -> datetime:
        return current

    sources = tuple(
        feeds.FeedSource(host, f"https://{host}/feed", "rss", r"https://.*")
        for host in ("healthy.example", "failed.example")
    )
    monkeypatch.setattr(feeds, "FEED_SOURCES", sources)

    async def public(_: str, __: frozenset[str]) -> None:
        return None

    monkeypatch.setattr(extraction, "assert_public_hostname", public)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls[request.url.host] += 1
        if request.url.host == "failed.example" and calls[request.url.host] == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, text="<rss><channel><title>No new stories</title></channel></rss>"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for attempt in range(2):
            execution = NewsExecution(
                news_database,
                root,
                uuid.uuid4(),
                current.date(),
                automatic=True,
                attempt=attempt,
                clock=clock,
            )
            with news_execution(execution):
                async with workflow_scope(news_database, execution.edition_date, "global"):
                    assert (
                        await feeds.discover_feed_candidates(
                            client, frozenset(source.hostname for source in sources), now=current
                        )
                        == []
                    )
            result = (await workflow_results(news_database, execution.run_id))["global"]
            assert result["state"] == ("waiting_retry" if attempt == 0 else "completed")
            current += timedelta(minutes=5)
    assert calls == {"healthy.example": 1, "failed.example": 2}
    async with news_database() as database:
        dependencies = (await database.scalars(select(NewsDependencyState))).all()
    assert all(row.state == "no_new_content" for row in dependencies)


@pytest.mark.integration
async def test_real_model_adapter_resumes_only_missing_locale(
    news_database: async_sessionmaker[AsyncSession],
) -> None:
    import json

    current = datetime(2026, 9, 11, 1, tzinfo=UTC)
    root = uuid.uuid4()
    calls: Counter[str] = Counter()

    def clock() -> datetime:
        return current

    def respond(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(json.loads(request.content)["messages"][1]["content"])
        locale = prompt["locale"]
        calls[locale] += 1
        if locale == "en" and calls[locale] == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"headline": "Headline", "summary": "Summary", "numeric_facts": []}
                            )
                        }
                    }
                ]
            },
        )

    client = DeepSeekClient(base_url="https://model.example", api_key="test", model="test")
    await client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    candidate = Candidate(
        id="a" * 64,
        url="https://news.example/story",
        hostname="news.example",
        source_name="News",
        headline="Headline",
    )
    try:
        for attempt in range(2):
            execution = NewsExecution(
                news_database,
                root,
                uuid.uuid4(),
                current.date(),
                automatic=True,
                attempt=attempt,
                clock=clock,
            )
            with news_execution(execution):
                async with workflow_scope(news_database, execution.edition_date, "global"):
                    for locale in ("zh-hant", "zh-hans", "en"):

                        async def summary(locale: str = locale) -> ModelCall:
                            return await client.summarize(candidate, "Source facts", locale)

                        try:
                            await model_step(
                                fingerprint(["source", locale]),
                                "summary",
                                summary,
                                lambda _: None,
                                locale=locale,
                            )
                        except NewsOperationError:
                            assert attempt == 0 and locale == "en"
            current += timedelta(minutes=5)
    finally:
        await client.aclose()
    assert calls == {"zh-hant": 1, "zh-hans": 1, "en": 2}
    async with news_database() as database:
        checkpoints = (await database.scalars(select(NewsCheckpoint))).all()
    assert all(checkpoint.result is not None for checkpoint in checkpoints)
    assert "Source facts" not in str([checkpoint.result for checkpoint in checkpoints])


@pytest.mark.integration
async def test_account_failure_stops_other_markets_without_another_paid_call(
    news_database: async_sessionmaker[AsyncSession],
) -> None:
    calls = 0

    async def unavailable() -> ModelCall:
        nonlocal calls
        calls += 1
        raise ModelCallError(
            "private upstream message",
            error_code="provider_http_402",
            input_digest="a" * 64,
            latency_ms=1,
        )

    for market in ["global", "us_equity"]:
        execution = NewsExecution(news_database, uuid.uuid4(), uuid.uuid4(), date(2026, 9, 11))
        with news_execution(execution):
            with pytest.raises(NewsOperationError):
                async with workflow_scope(news_database, execution.edition_date, market):
                    await model_step(fingerprint(market), "selection", unavailable, lambda _: None)
        results = await workflow_results(news_database, execution.run_id)
        assert results[market]["state"] == "needs_attention"
        assert "private upstream message" not in str(results)
    assert calls == 1


@pytest.mark.integration
async def test_validation_repair_budget_survives_resume(
    news_database: async_sessionmaker[AsyncSession],
) -> None:
    root = uuid.uuid4()
    calls = 0

    async def invalid() -> ModelCall:
        nonlocal calls
        calls += 1
        raise ModelCallError(
            "invalid number",
            error_code="summary_ungrounded_number",
            input_digest="a" * 64,
            latency_ms=1,
        )

    for _ in range(2):
        execution = NewsExecution(news_database, root, uuid.uuid4(), date(2026, 9, 11))
        with news_execution(execution):
            async with workflow_scope(news_database, execution.edition_date, "global"):
                with pytest.raises(NewsOperationError):
                    await model_step(fingerprint("same input"), "summary", invalid, lambda _: None)
    assert calls == 2


@pytest.mark.integration
async def test_interruption_during_correction_does_not_allow_another_correction(
    news_database: async_sessionmaker[AsyncSession],
) -> None:
    root = uuid.uuid4()
    calls = 0

    async def interrupted() -> ModelCall:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModelCallError(
                "invalid", error_code="summary_invalid_json", input_digest="a" * 64, latency_ms=1
            )
        raise asyncio.CancelledError

    execution = NewsExecution(news_database, root, uuid.uuid4(), date(2026, 9, 11))
    with news_execution(execution), pytest.raises(asyncio.CancelledError):
        async with workflow_scope(news_database, execution.edition_date, "global"):
            await model_step(
                fingerprint("interrupted correction"), "summary", interrupted, lambda _: None
            )
    with news_execution(execution):
        async with workflow_scope(news_database, execution.edition_date, "global"):
            with pytest.raises(NewsOperationError, match="exhausted"):
                await model_step(
                    fingerprint("interrupted correction"), "summary", interrupted, lambda _: None
                )
    assert calls == 2
