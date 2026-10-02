import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.models import Base
from daily_insights_api.modules.newsroom import queue
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomLlmCall,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.queue import (
    Claim,
    FatalStageError,
    RetryableStageError,
    claim,
    run_claimed,
)

pytestmark = pytest.mark.integration


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


async def _article(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as database:
        source = NewsroomSource(
            key="example", name="Example", kind="rss", hostname="example.com", markets=["global"]
        )
        database.add(source)
        await database.flush()
        url = f"https://example.com/{uuid.uuid4()}"
        article = NewsroomArticle(
            source_id=source.id,
            url=url,
            url_hash=uuid.uuid4().hex * 2,
            title="Headline",
            edition_date=date(2026, 10, 1),
            embed_status="pending",
        )
        database.add(article)
        await database.commit()
        return article.id


async def _row(
    session_factory: async_sessionmaker[AsyncSession], article_id: uuid.UUID
) -> NewsroomArticle:
    async with session_factory() as database:
        row = await database.get(NewsroomArticle, article_id)
        assert row is not None
        return row


async def test_claim_leases_row_and_success_settles_it(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    article_id = await _article(newsroom_database)
    async with newsroom_database() as database:
        claims = await claim(database, queue.EMBED)
    assert [item.row_id for item in claims] == [article_id]
    # Leased: a second claim finds nothing until the lease expires.
    async with newsroom_database() as database:
        assert await claim(database, queue.EMBED) == []

    async def handler(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
        del database, claim_
        return {"embedding": [0.0] * 1536}

    assert await run_claimed(newsroom_database, claims[0], handler) == "done"
    row = await _row(newsroom_database, article_id)
    assert row.embed_status == "done"
    assert row.embed_next_attempt_at is None
    assert row.embedding is not None


async def test_retryable_failure_backs_off_then_fails_when_exhausted(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    article_id = await _article(newsroom_database)

    async def handler(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
        database.add(NewsroomLlmCall(stage="embed", model="m", latency_ms=1))
        error = RetryableStageError("embed_provider_timeout")
        error.audit_rows.append(NewsroomLlmCall(stage="embed", model="m", latency_ms=2))
        raise error

    for attempt in range(1, queue.EMBED.max_attempts + 1):
        future = datetime.now(UTC) + timedelta(days=1)
        async with newsroom_database() as database:
            claims = await claim(database, queue.EMBED, now=future)
        assert len(claims) == 1 and claims[0].attempt == attempt
        await run_claimed(newsroom_database, claims[0], handler)
    row = await _row(newsroom_database, article_id)
    assert row.embed_status == "failed"
    assert row.embed_error_code == "embed_provider_timeout"
    async with newsroom_database() as database:
        # The audit row attached to the error survives; the one added in the
        # handler's own session was rolled back with the stage.
        latencies = (await database.scalars(select(NewsroomLlmCall.latency_ms))).all()
    assert sorted(set(latencies)) == [2]
    assert len(latencies) == queue.EMBED.max_attempts


async def test_fatal_failure_fails_immediately_and_notifies(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    article_id = await _article(newsroom_database)
    notified: list[str] = []

    async def handler(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
        raise FatalStageError("embed_provider_http_401")

    async def on_fatal(claim_: Claim, error: FatalStageError) -> None:
        notified.append(error.code)

    async with newsroom_database() as database:
        claims = await claim(database, queue.EMBED)
    assert await run_claimed(newsroom_database, claims[0], handler, on_fatal=on_fatal) == "fatal"
    assert (await _row(newsroom_database, article_id)).embed_status == "failed"
    assert notified == ["embed_provider_http_401"]


async def test_stale_claim_cannot_overwrite_a_newer_attempt(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    await _article(newsroom_database)
    async with newsroom_database() as database:
        stale = (await claim(database, queue.EMBED))[0]
    later = datetime.now(UTC) + queue.EMBED.lease + timedelta(seconds=1)
    async with newsroom_database() as database:
        fresh = (await claim(database, queue.EMBED, now=later))[0]
    assert fresh.attempt == stale.attempt + 1

    async def handler(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
        return {}

    assert await run_claimed(newsroom_database, stale, handler) == "fenced"
    assert await run_claimed(newsroom_database, fresh, handler) == "done"


async def test_enqueue_resets_a_failed_row(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    article_id = await _article(newsroom_database)
    async with newsroom_database() as database:
        claims = await claim(database, queue.EMBED)

    async def handler(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
        raise FatalStageError("embed_provider_http_403", notify=False)

    await run_claimed(newsroom_database, claims[0], handler)
    async with newsroom_database() as database:
        await queue.enqueue(database, queue.EMBED, [article_id])
        await database.commit()
        pending = await database.scalar(
            select(func.count())
            .select_from(NewsroomArticle)
            .where(NewsroomArticle.embed_status == "pending", NewsroomArticle.embed_attempts == 0)
        )
    assert pending == 1


async def test_handler_can_settle_an_alternative_terminal_status(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    article_id = await _article(newsroom_database)
    async with newsroom_database() as database:
        claims = await claim(database, queue.EMBED)

    async def handler(database: AsyncSession, claim_: Claim) -> dict[str, Any]:
        return {"embed_status": "failed", "embed_error_code": "embed_input_empty"}

    assert await run_claimed(newsroom_database, claims[0], handler) == "done"
    row = await _row(newsroom_database, article_id)
    assert (row.embed_status, row.embed_error_code) == ("failed", "embed_input_empty")
