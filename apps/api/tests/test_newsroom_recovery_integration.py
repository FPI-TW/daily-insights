import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.models import Base
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.newsroom import assembly, clock, publishing, recovery
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
from daily_insights_api.scripts import run_newsroom_recover

pytestmark = pytest.mark.integration

OUTAGE = date(2026, 10, 25)
EARLIER = date(2026, 10, 20)
EMBEDDING_MODEL = "text-embedding-3-large"
TRANSLATE_MODEL = "deepseek-chat"
# After the 2026-10-25 12:00 late-fill deadline.
NOW = datetime(2026, 10, 27, 3, tzinfo=UTC)
VECTOR = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)


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


async def _source(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as database:
        source = NewsroomSource(
            key=f"src-{uuid.uuid4().hex[:8]}",
            name="Source",
            kind="rss",
            hostname="source.example.com",
            markets=["global"],
        )
        database.add(source)
        await database.commit()
        return source.id


async def _article(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
    *,
    edition_date: date = OUTAGE,
    embedding: bool = True,
    **values: Any,
) -> uuid.UUID:
    async with session_factory() as database:
        # Triage failed with the outage's code unless ``values`` say otherwise.
        fields: dict[str, Any] = {
            "fetch_status": "done",
            "embed_status": "done",
            "triage_status": "failed",
            "triage_attempts": 1,
            "triage_error_code": "triage_provider_http_402",
        } | values
        article = NewsroomArticle(
            source_id=source_id,
            url=f"https://example.com/{uuid.uuid4()}",
            url_hash=uuid.uuid4().hex * 2,
            title="Fed holds rates",
            edition_date=edition_date,
            first_seen_at=datetime(2026, 10, 24, 12, tzinfo=UTC),
            embedding=VECTOR if embedding else None,
            **fields,
        )
        database.add(article)
        await database.commit()
        return article.id


async def _call(
    session_factory: async_sessionmaker[AsyncSession],
    article_id: uuid.UUID,
    *,
    model: str = TRANSLATE_MODEL,
    error_code: str | None = "embed_provider_http_402",
    at: datetime = datetime(2026, 10, 24, 12, tzinfo=UTC),
) -> None:
    async with session_factory() as database:
        database.add(
            NewsroomLlmCall(
                stage="embed",
                subject_id=article_id,
                model=model,
                latency_ms=1,
                error_code=error_code,
                created_at=at,
            )
        )
        await database.commit()


async def _get(session_factory: async_sessionmaker[AsyncSession], model: Any, row_id: Any) -> Any:
    async with session_factory() as database:
        row = await database.get(model, row_id)
        assert row is not None
        return row


def _counts(plan: recovery.Plan) -> list[tuple[str, str | None, str, int]]:
    return [
        (group.edition_date.isoformat(), group.queue, group.reason, len(group.row_ids))
        for group in plan.groups
    ]


# --- requeue -----------------------------------------------------------------------


async def test_requeue_triage_sends_unusable_embeddings_back_to_embed(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source = await _source(newsroom_database)
    plain = await _article(newsroom_database, source)
    missing = await _article(newsroom_database, source, embedding=False)
    untranslated = await _article(newsroom_database, source)
    await _call(newsroom_database, untranslated)
    # A later retry translated it: its vector is fine.
    retranslated = await _article(newsroom_database, source)
    await _call(newsroom_database, retranslated)
    await _call(
        newsroom_database,
        retranslated,
        error_code=None,
        at=datetime(2026, 10, 24, 13, tzinfo=UTC),
    )
    # The embedding call itself failing is not a skipped translation.
    embedding_failed = await _article(newsroom_database, source)
    await _call(newsroom_database, embedding_failed, model=EMBEDDING_MODEL)
    other_code = await _article(
        newsroom_database, source, triage_error_code="triage_schema_invalid"
    )
    earlier = await _article(newsroom_database, source, edition_date=EARLIER)

    async with newsroom_database() as database:
        plan = await recovery.requeue(
            database,
            "triage",
            ["triage_provider_http_402"],
            start=OUTAGE,
            embedding_model=EMBEDDING_MODEL,
            now=NOW,
        )
    assert not plan.applied
    assert _counts(plan) == [
        ("2026-10-25", "embed", "embedding_missing", 1),
        ("2026-10-25", "embed", "untranslated_embedding", 1),
        ("2026-10-25", "triage", "failed", 3),
    ]
    assert plan.queued == 5
    assert (await _get(newsroom_database, NewsroomArticle, plain)).triage_status == "failed"

    async with newsroom_database() as database:
        plan = await recovery.requeue(
            database,
            "triage",
            ["triage_provider_http_402"],
            start=OUTAGE,
            embedding_model=EMBEDDING_MODEL,
            apply=True,
            now=NOW,
        )
        await database.commit()
    assert plan.applied
    for article_id in (plain, retranslated, embedding_failed):
        article = await _get(newsroom_database, NewsroomArticle, article_id)
        assert (article.triage_status, article.triage_attempts, article.triage_error_code) == (
            "pending",
            0,
            None,
        )
        assert article.embed_status == "done"
    for article_id in (missing, untranslated):
        article = await _get(newsroom_database, NewsroomArticle, article_id)
        assert (article.embed_status, article.triage_status, article.triage_error_code) == (
            "pending",
            "idle",
            None,
        )
    for article_id in (other_code, earlier):
        article = await _get(newsroom_database, NewsroomArticle, article_id)
        assert article.triage_status == "failed"


async def test_requeue_leaves_a_routed_article_whose_embed_is_in_flight(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source = await _source(newsroom_database)
    article_id = await _article(
        newsroom_database,
        source,
        embedding=False,
        embed_status="pending",
        embed_next_attempt_at=NOW + timedelta(minutes=5),
    )
    async with newsroom_database() as database:
        plan = await recovery.requeue(
            database,
            "triage",
            ["triage_provider_http_402"],
            embedding_model=EMBEDDING_MODEL,
            apply=True,
            now=NOW,
        )
        await database.commit()
    assert _counts(plan) == [("2026-10-25", None, "embed_in_flight", 1)]
    article = await _get(newsroom_database, NewsroomArticle, article_id)
    assert (article.embed_next_attempt_at, article.triage_status) == (
        NOW + timedelta(minutes=5),
        "failed",
    )


async def test_requeue_events_and_items(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        failed = NewsroomEvent(
            edition_date=OUTAGE,
            working_title="Failed",
            analysis_status="failed",
            analysis_error_code="analysis_provider_http_402",
            en_status="failed",
            en_error_code="translate_provider_http_402",
        )
        merged_into = NewsroomEvent(
            edition_date=OUTAGE, working_title="Kept", analysis_status="ready"
        )
        database.add_all([failed, merged_into])
        await database.flush()
        merged = NewsroomEvent(
            edition_date=OUTAGE,
            working_title="Merged",
            status="merged",
            merged_into_id=merged_into.id,
            analysis_status="failed",
            analysis_error_code="analysis_provider_http_402",
        )
        edition = NewsroomEdition(
            edition_date=OUTAGE,
            market_code="global",
            auto_publish_at=clock.auto_publish_at(OUTAGE),
            late_fill_deadline=clock.late_fill_deadline(OUTAGE),
        )
        database.add_all([merged, edition])
        await database.flush()
        why_failed = {
            "why_status": "failed",
            "why_error_code": "why_provider_http_402",
        }
        ready_item = NewsroomEditionItem(
            edition_id=edition.id, event_id=merged_into.id, rank=1, **why_failed
        )
        pending_item = NewsroomEditionItem(
            edition_id=edition.id, event_id=failed.id, rank=2, **why_failed
        )
        database.add_all([ready_item, pending_item])
        await database.commit()

    async with newsroom_database() as database:
        analysis = await recovery.requeue(
            database,
            "analysis",
            ["analysis_provider_http_402"],
            embedding_model=EMBEDDING_MODEL,
            apply=True,
        )
        english = await recovery.requeue(
            database,
            "en",
            ["translate_provider_http_402"],
            embedding_model=EMBEDDING_MODEL,
            apply=True,
        )
        why = await recovery.requeue(
            database, "why", ["why_provider_http_402"], embedding_model=EMBEDDING_MODEL, apply=True
        )
        await database.commit()
    assert _counts(analysis) == [
        ("2026-10-25", "analysis", "failed", 1),
        ("2026-10-25", None, "event_merged", 1),
    ]
    assert _counts(english) == [("2026-10-25", "en", "failed", 1)]
    assert _counts(why) == [
        ("2026-10-25", "why", "failed", 1),
        ("2026-10-25", None, "event_not_ready", 1),
    ]
    event = await _get(newsroom_database, NewsroomEvent, failed.id)
    assert (event.analysis_status, event.en_status) == ("pending", "pending")
    assert (await _get(newsroom_database, NewsroomEvent, merged.id)).analysis_status == "failed"
    assert (await _get(newsroom_database, NewsroomEditionItem, ready_item.id)).why_status == (
        "pending"
    )
    assert (await _get(newsroom_database, NewsroomEditionItem, pending_item.id)).why_status == (
        "failed"
    )


async def test_requeue_rejects_an_unknown_stage_or_no_code(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        with pytest.raises(ValueError, match="unknown stage"):
            await recovery.requeue(database, "editor", ["x"], embedding_model=EMBEDDING_MODEL)
        with pytest.raises(ValueError, match="error code"):
            await recovery.requeue(database, "embed", [], embedding_model=EMBEDDING_MODEL)


# --- reembed-untranslated ----------------------------------------------------------


async def test_reembed_untranslated_requeues_only_untriaged_articles(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source = await _source(newsroom_database)
    failed = await _article(newsroom_database, source)
    triaged = await _article(
        newsroom_database, source, triage_status="done", triage_error_code=None
    )
    triaging = await _article(
        newsroom_database,
        source,
        triage_status="pending",
        triage_error_code=None,
        triage_next_attempt_at=NOW + timedelta(minutes=3),
    )
    queued = await _article(newsroom_database, source, triage_status="pending")
    translated = await _article(newsroom_database, source)
    for article_id in (failed, triaged, triaging, queued):
        await _call(newsroom_database, article_id)
    await _call(newsroom_database, translated, error_code=None)

    async with newsroom_database() as database:
        plan = await recovery.reembed_untranslated(
            database, embedding_model=EMBEDDING_MODEL, apply=True, now=NOW
        )
        await database.commit()
    assert _counts(plan) == [
        ("2026-10-25", "embed", "untranslated_embedding", 2),
        ("2026-10-25", None, "already_triaged", 1),
        ("2026-10-25", None, "triage_in_flight", 1),
    ]
    for article_id in (failed, queued):
        article = await _get(newsroom_database, NewsroomArticle, article_id)
        assert (article.embed_status, article.embed_attempts, article.triage_status) == (
            "pending",
            0,
            "idle",
        )
        # The old vector stays until the new one replaces it.
        assert article.embedding is not None
    assert (await _get(newsroom_database, NewsroomArticle, triaged)).embed_status == "done"
    assert (await _get(newsroom_database, NewsroomArticle, triaging)).triage_status == "pending"
    assert (await _get(newsroom_database, NewsroomArticle, translated)).embed_status == "done"


# --- reopen ------------------------------------------------------------------------


async def _published_edition(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    market: str = "global",
    abandoned: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A 2026-10-25 edition published at 09:00 and closed at 12:00, and its event."""
    async with session_factory() as database:
        event = NewsroomEvent(
            edition_date=OUTAGE,
            working_title="Fed holds",
            analysis_status="failed",
            analysis_error_code="analysis_provider_http_402",
        )
        edition = NewsroomEdition(
            edition_date=OUTAGE,
            market_code=market,
            auto_publish_at=clock.auto_publish_at(OUTAGE),
            late_fill_deadline=clock.late_fill_deadline(OUTAGE),
            assembled_at=clock.at_taipei(OUTAGE, datetime.min.time()),
            status="published",
            published_at=clock.auto_publish_at(OUTAGE),
            late_fill_closed_at=clock.late_fill_deadline(OUTAGE),
        )
        database.add_all([event, edition])
        await database.flush()
        database.add(
            NewsroomEditionItem(
                edition_id=edition.id,
                event_id=event.id,
                rank=1,
                abandoned_at=clock.late_fill_deadline(OUTAGE) if abandoned else None,
            )
        )
        await database.commit()
        return edition.id, event.id


async def _admin(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as database:
        user = User(
            email="ops@example.com",
            display_name="Ops",
            password_hash="unused",
            must_change_password=False,
            system_role=SystemRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        database.add(user)
        await database.commit()
        return user.id


async def test_reopen_returns_an_empty_edition_to_draft_until_reassembly(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    edition_id, event_id = await _published_edition(newsroom_database)
    user_id = await _admin(newsroom_database)
    six_pm = clock.at_taipei(date(2026, 10, 27), datetime.min.time().replace(hour=18))

    async with newsroom_database() as database:
        with pytest.raises(ValueError, match="late-fill deadline has passed"):
            await publishing.reopen_edition(database, edition_id, now=NOW)
        with pytest.raises(ValueError, match="already passed"):
            await publishing.reopen_edition(
                database, edition_id, late_fill_deadline=NOW - timedelta(minutes=1), now=NOW
            )
        check = await publishing.reopen_edition(
            database, edition_id, late_fill_deadline=six_pm, user_id=user_id, now=NOW
        )
        await database.commit()
    assert (check.action, check.live_items, check.abandoned_items) == ("reopen", 0, 1)
    edition = await _get(newsroom_database, NewsroomEdition, edition_id)
    assert (edition.status, edition.published_at, edition.assembled_at) == ("draft", None, None)
    assert (edition.late_fill_deadline, edition.late_fill_closed_at) == (six_pm, None)
    async with newsroom_database() as database:
        items = (await database.scalars(select(NewsroomEditionItem))).all()
        assert [item.abandoned_at for item in items] == [None]
        log = (await database.scalars(select(NewsroomEditLog))).one()
    assert (log.entity_id, log.action, log.user_id) == (edition_id, "reopen", user_id)
    assert log.before is not None and log.before["status"] == "published"

    # Its item is live again, yet nothing publishes or holds it before re-assembly.
    async with newsroom_database() as database:
        assert await publishing.auto_publish_due(database, now=NOW) == 0
        assert await publishing.hold_empty_due(database, now=NOW) == []
        # Re-assembly rebuilds the draft and queues the failed analysis again.
        await assembly.write_draft(
            database,
            edition_date=OUTAGE,
            market="global",
            picks=[assembly.Pick(event_id, 4, 1.0)],
            selection_mode="editor",
            ignored_pending_triage=0,
            now=NOW,
        )
        await database.commit()
    assert (await _get(newsroom_database, NewsroomEvent, event_id)).analysis_status == "pending"
    async with newsroom_database() as database:
        assert await publishing.auto_publish_due(database, now=NOW) == 1
        await database.commit()
    edition = await _get(newsroom_database, NewsroomEdition, edition_id)
    assert (edition.status, edition.published_at) == ("published", NOW)
    # The late fill waits for the new deadline.
    async with newsroom_database() as database:
        assert await publishing.close_late_fill(database, now=NOW) == []


async def test_reopen_needs_force_for_an_edition_with_live_items(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    edition_id, _ = await _published_edition(newsroom_database, abandoned=False)
    later = NOW + timedelta(hours=3)
    async with newsroom_database() as database:
        with pytest.raises(ValueError, match=r"1 live item\(s\)"):
            await publishing.reopen_edition(database, edition_id, late_fill_deadline=later, now=NOW)
        check = await publishing.reopen_edition(
            database, edition_id, force=True, late_fill_deadline=later, now=NOW
        )
        # A draft is left alone.
        again = await publishing.reopen_edition(database, edition_id, now=NOW)
        await database.commit()
        assert (await database.scalars(select(NewsroomEditLog))).all() == []
    assert (check.action, check.live_items) == ("reopen", 1)
    assert (again.action, again.reason) == ("skip", "already a draft")
    assert (await _get(newsroom_database, NewsroomEdition, edition_id)).status == "draft"


# --- script ------------------------------------------------------------------------


async def _run(
    session_factory: async_sessionmaker[AsyncSession], *args: str
) -> tuple[int, dict[str, Any]]:
    settings = Settings(
        _env_file=None, environment="test", newsroom_embedding_model=EMBEDDING_MODEL
    )
    options = run_newsroom_recover.parse_args(list(args))
    return await run_newsroom_recover.run(session_factory, settings, options, now=NOW)


async def test_recover_script_counts_first_and_writes_with_apply(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source = await _source(newsroom_database)
    article_id = await _article(newsroom_database, source)
    await _call(newsroom_database, article_id)
    args = ("reembed-untranslated", "--from", "2026-10-25", "--to", "2026-10-27")

    code, report = await _run(newsroom_database, *args)
    assert code == 0
    assert report == {
        "command": "reembed-untranslated",
        "mode": "dry_run",
        "embedding_model": EMBEDDING_MODEL,
        "from": "2026-10-25",
        "to": "2026-10-27",
        "applied": False,
        "queued": 1,
        "groups": [
            {
                "edition_date": "2026-10-25",
                "queue": "embed",
                "reason": "untranslated_embedding",
                "count": 1,
            }
        ],
        "hint": "dry run: nothing was written; add --apply to write",
    }
    assert (await _get(newsroom_database, NewsroomArticle, article_id)).embed_status == "done"

    code, report = await _run(newsroom_database, *args, "--apply")
    assert (code, report["mode"], report["applied"]) == (0, "apply", True)
    assert (await _get(newsroom_database, NewsroomArticle, article_id)).embed_status == "pending"

    code, report = await _run(
        newsroom_database, "requeue", "--stage", "embed", "--error-code", "embed_input_empty"
    )
    assert (code, report["stage"], report["queued"], report["groups"]) == (0, "embed", 0, [])


async def test_recover_script_reopen_is_all_or_nothing(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    empty_id, _ = await _published_edition(newsroom_database, market="global")
    full_id, _ = await _published_edition(newsroom_database, market="us_equity", abandoned=False)
    await _admin(newsroom_database)
    args = ("reopen", "--edition-date", "2026-10-25", "--extend-late-fill", "2026-10-27T18:00")

    code, report = await _run(newsroom_database, *args, "--apply")
    assert (code, report["applied"]) == (1, False)
    assert report["error"] == "an edition cannot be reopened as asked; nothing was changed"
    assert [
        (edition["market"], edition["action"], edition["live_items"])
        for edition in report["editions"]
    ] == [("global", "reopen", 0), ("us_equity", "refuse", 1)]
    assert report["editions"][0]["new_late_fill_deadline"] == "2026-10-27T18:00:00+08:00"
    assert (await _get(newsroom_database, NewsroomEdition, empty_id)).status == "published"

    with pytest.raises(RuntimeError, match="not an active admin"):
        await _run(newsroom_database, *args, "--market", "global", "--actor", "x@example.com")

    code, report = await _run(
        newsroom_database, *args, "--market", "global", "--actor", "OPS@example.com", "--apply"
    )
    assert (code, report["applied"], report["edit_log"]) == (0, True, True)
    assert "assemble-newsroom EDITION_DATE=2026-10-25" in report["next"]
    assert (await _get(newsroom_database, NewsroomEdition, empty_id)).status == "draft"
    assert (await _get(newsroom_database, NewsroomEdition, full_id)).status == "published"

    code, report = await _run(newsroom_database, "reopen", "--edition-date", "2026-10-26")
    assert (code, report["error"]) == (1, "no edition on that date")


async def test_recover_script_dry_run_cannot_write(
    newsroom_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def writes(database: AsyncSession, **_: Any) -> recovery.Plan:
        await database.execute(update(NewsroomArticle).values(title="x"))
        return recovery.Plan()

    monkeypatch.setattr(recovery, "reembed_untranslated", writes)
    with pytest.raises(Exception, match="read-only transaction"):
        await _run(newsroom_database, "reembed-untranslated")
    code, report = await _run(newsroom_database, "reembed-untranslated", "--apply")
    assert (code, report["mode"]) == (0, "apply")
