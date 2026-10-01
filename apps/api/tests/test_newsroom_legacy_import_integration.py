"""Migration 20261001_0034: legacy daily-news history moves into the newsroom tables.

The legacy rows are written with plain SQL against the schema as of revision
20261001_0033, because the legacy ORM models are removed by the cutover.
"""

import hashlib
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from conftest import reset_database_schema
from sqlalchemy import Connection, create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEvent,
)
from daily_insights_api.modules.newsroom.public_api import latest_edition, visible_item_texts
from daily_insights_api.modules.newsroom.translation import current_zh_hant_digest, to_zh_hans

pytestmark = pytest.mark.integration

BEFORE_IMPORT = "20261001_0033"
IMPORT = "20261001_0034"
DROP_LEGACY = "20261001_0035"
LEGACY_TABLES = ("news_editions", "news_items", "news_presentations", "news_candidates")
GENERATED_AT = datetime(2026, 9, 29, 0, 5, tzinfo=UTC)


def _alembic(database_url: str, action: str, target: str) -> None:
    previous = os.environ.get("DAILY_INSIGHTS_DATABASE_URL")
    os.environ["DAILY_INSIGHTS_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        getattr(command, action)(config, target)
    finally:
        if previous is None:
            os.environ.pop("DAILY_INSIGHTS_DATABASE_URL", None)
        else:
            os.environ["DAILY_INSIGHTS_DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest.fixture
def database_url() -> Iterator[str]:
    url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required for integration tests")
    reset_database_schema(url)
    _alembic(url, "upgrade", BEFORE_IMPORT)
    yield url
    reset_database_schema(url)


def _edition(
    connection: Connection, day: date, market: str, revision: int, status: str = "complete"
) -> uuid.UUID:
    edition_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO news_editions (id, edition_date, market_code, revision, input_digest, "
            "derivation_version, model_name, prompt_version, status, generated_at) VALUES "
            "(:id, :day, :market, :revision, :digest, 'v1', 'deepseek-chat', 'p1', :status, "
            ":generated_at)"
        ),
        {
            "id": edition_id,
            "day": day,
            "market": market,
            "revision": revision,
            "digest": "0" * 64,
            "status": status,
            "generated_at": GENERATED_AT,
        },
    )
    return edition_id


def _item(
    connection: Connection,
    edition_id: uuid.UUID,
    rank: int,
    url: str,
    texts: dict[str, tuple[str, str]],
    *,
    importance: int = 4,
    hidden: bool = False,
    event_key: str | None = None,
) -> uuid.UUID:
    item_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO news_items (id, edition_id, rank, topic, source_name, source_hostname, "
            "source_url, source_headline, source_published_at, importance, content_digest, "
            "numeric_facts, event_key, hidden_at) VALUES (:id, :edition, :rank, 'markets', "
            "'Example', 'example.com', :url, :headline, :published, :importance, :digest, "
            "'[]', :event_key, :hidden_at)"
        ),
        {
            "id": item_id,
            "edition": edition_id,
            "rank": rank,
            "url": url,
            "headline": f"Source headline {rank}",
            "published": GENERATED_AT,
            "importance": importance,
            "digest": "1" * 64,
            "event_key": event_key,
            "hidden_at": GENERATED_AT if hidden else None,
        },
    )
    for locale, (headline, summary) in texts.items():
        connection.execute(
            text(
                "INSERT INTO news_presentations (item_id, locale, headline, summary) "
                "VALUES (:item, :locale, :headline, :summary)"
            ),
            {"item": item_id, "locale": locale, "headline": headline, "summary": summary},
        )
    return item_id


def _trilingual(label: str) -> dict[str, tuple[str, str]]:
    return {
        "zh-hant": (f"{label} 臺灣標題", f"{label} 繁體摘要。"),
        "zh-hans": (f"{label} 台湾标题", f"{label} 简体摘要。"),
        "en": (f"{label} English headline", f"{label} English summary."),
    }


def _seed_legacy(url: str) -> dict[str, Any]:
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            day = date(2026, 9, 29)
            # Older revision of the same day: superseded by revision 2.
            old = _edition(connection, day, "global", 1)
            _item(connection, old, 1, "https://example.com/old", _trilingual("old"))
            current = _edition(connection, day, "global", 2, "partial")
            _item(
                connection,
                current,
                1,
                "https://example.com/a",
                _trilingual("A"),
                importance=5,
            )
            # No zh-hans and no English: zh-hans is converted from zh-hant.
            _item(
                connection,
                current,
                2,
                "https://example.com/b",
                {"zh-hant": ("B 臺灣發佈頭條", "B 只有繁體摘要。")},
                importance=3,
            )
            _item(
                connection, current, 3, "https://example.com/hidden", _trilingual("H"), hidden=True
            )
            # An unavailable revision never reached readers.
            unavailable = _edition(connection, day, "global", 3, "unavailable")
            _item(connection, unavailable, 1, "https://example.com/never", _trilingual("N"))
            # A hidden tw_equity item suppresses the same URL in other editions.
            earlier = _edition(connection, date(2026, 9, 28), "tw_equity", 1)
            _item(
                connection,
                earlier,
                1,
                "https://example.com/suppressed",
                _trilingual("S"),
                hidden=True,
            )
            tw = _edition(connection, day, "tw_equity", 1)
            _item(connection, tw, 1, "https://example.com/suppressed", _trilingual("S2"))
            _item(connection, tw, 2, "https://example.com/linked", _trilingual("T"))
            _item(connection, tw, 3, "https://example.com/taken", _trilingual("U"))
            # A date the new pipeline already owns is left alone.
            us = _edition(connection, date(2026, 9, 30), "us_equity", 1)
            _item(connection, us, 1, "https://example.com/us", _trilingual("US"))
            connection.execute(
                text(
                    "INSERT INTO newsroom_editions (id, edition_date, market_code, status, "
                    "selection_mode, auto_publish_at, late_fill_deadline) VALUES "
                    "(:id, '2026-09-30', 'us_equity', 'draft', 'editor', :at, :at)"
                ),
                {"id": uuid.uuid4(), "at": GENERATED_AT},
            )
            source_id = connection.scalar(
                text("SELECT id FROM newsroom_sources WHERE key = 'cnbc-top-news'")
            )
            event_id = uuid.uuid4()
            connection.execute(
                text(
                    "INSERT INTO newsroom_events (id, edition_date, working_title) "
                    "VALUES (:id, '2026-09-29', 'existing event')"
                ),
                {"id": event_id},
            )
            for article_url, article_event in (
                ("https://example.com/linked", None),
                ("https://example.com/taken", event_id),
            ):
                connection.execute(
                    text(
                        "INSERT INTO newsroom_articles (id, source_id, url, url_hash, title, "
                        "edition_date, event_id) VALUES (:id, :source, :url, :hash, 'Existing', "
                        "'2026-09-29', :event)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "source": source_id,
                        "url": article_url,
                        "hash": hashlib.sha256(article_url.encode()).hexdigest(),
                        "event": article_event,
                    },
                )
            return {"taken_event_id": event_id}
    finally:
        engine.dispose()


def _seed_legacy_runs(url: str) -> dict[str, uuid.UUID]:
    """A legacy news job still waiting to run and one that already finished."""
    engine = create_engine(url)
    ids: dict[str, uuid.UUID] = {}
    try:
        with engine.begin() as connection:
            for name, status in (("pending", "running"), ("finished", "succeeded")):
                job_id, function_id = uuid.uuid4(), uuid.uuid4()
                connection.execute(
                    text(
                        "INSERT INTO job_runs (id, job_key, kind, trigger, registry_version, "
                        "registry_snapshot, edition_date, status) VALUES (:id, "
                        "'news_daily_update', 'function', 'manual', 'test', '{}', "
                        "'2026-09-29', :status)"
                    ),
                    {"id": job_id, "status": status},
                )
                connection.execute(
                    text(
                        "INSERT INTO function_runs (id, job_run_id, function_key, provider_key, "
                        "scope, status) VALUES (:id, :job, 'news_publish', "
                        "'internal_services', '{}', :status)"
                    ),
                    {
                        "id": function_id,
                        "job": job_id,
                        "status": "retry_wait" if name == "pending" else "succeeded",
                    },
                )
                ids[f"{name}_job"], ids[f"{name}_function"] = job_id, function_id
    finally:
        engine.dispose()
    return ids


def _run_table(key: str) -> str:
    return "job_runs" if key.endswith("job") else "function_runs"


def _legacy_state(url: str, ids: dict[str, uuid.UUID]) -> dict[str, Any]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            tables = set(
                connection.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            )
            statuses = {
                key: connection.scalar(
                    text(f"SELECT status FROM {_run_table(key)} WHERE id = :id"), {"id": value}
                )
                for key, value in ids.items()
            }
            return {"tables": tables, "statuses": statuses}
    finally:
        engine.dispose()


async def _sessions(url: str) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_legacy_editions_become_readable_newsroom_editions(database_url: str) -> None:
    seeded = _seed_legacy(database_url)
    _alembic(database_url, "upgrade", IMPORT)
    engine, sessions = await _sessions(database_url)
    try:
        async with sessions() as database:
            legacy_editions = (
                await database.scalars(
                    select(NewsroomEdition)
                    .where(NewsroomEdition.selection_mode == "legacy")
                    .order_by(NewsroomEdition.market_code)
                )
            ).all()
            assert [(row.edition_date, row.market_code) for row in legacy_editions] == [
                (date(2026, 9, 29), "global"),
                (date(2026, 9, 29), "tw_equity"),
            ]
            for edition in legacy_editions:
                assert edition.status == "published"
                assert edition.published_at == GENERATED_AT
                assert edition.auto_publish_at == datetime(2026, 9, 29, 1, tzinfo=UTC)
                assert edition.late_fill_deadline == datetime(2026, 9, 29, 4, tzinfo=UTC)
                assert edition.late_fill_closed_at == edition.late_fill_deadline

            zh_hant = await latest_edition(database, "global", "zh-hant", today=date(2026, 10, 1))
            assert zh_hant.edition_date == date(2026, 9, 29)
            assert zh_hant.is_today is False
            assert [(item.rank, item.stars, item.headline, item.why) for item in zh_hant.items] == [
                (1, 5, "A 臺灣標題", None),
                (2, 3, "B 臺灣發佈頭條", None),
            ]
            assert [source.url for source in zh_hant.items[0].sources] == ["https://example.com/a"]

            zh_hans = await latest_edition(database, "global", "zh-hans", today=date(2026, 9, 29))
            assert [item.headline for item in zh_hans.items] == [
                "A 台湾标题",
                to_zh_hans("B 臺灣發佈頭條"),
            ]
            assert zh_hans.items[1].summary == to_zh_hans("B 只有繁體摘要。")

            english = await latest_edition(database, "global", "en", today=date(2026, 9, 29))
            assert [(item.headline, item.summary) for item in english.items] == [
                ("A English headline", "A English summary.")
            ]
            # The chat page context reads the same visible items.
            assert english.edition_id is not None
            assert await visible_item_texts(database, english.edition_id, "en") == [
                ("A English headline", "A English summary.", None)
            ]

            events = (
                await database.scalars(
                    select(NewsroomEvent).where(NewsroomEvent.created_by == "legacy")
                )
            ).all()
            assert len(events) == 4
            for event in events:
                assert event.analysis_status == "ready"
                if event.en_status == "ready":
                    # The migration's frozen digest must match the reader's check.
                    assert event.en_source_digest == await current_zh_hant_digest(
                        database, event.id
                    )
                else:
                    assert event.en_source_digest is None
            items = (
                await database.scalars(
                    select(NewsroomEditionItem).where(NewsroomEditionItem.origin == "legacy")
                )
            ).all()
            assert {(item.why_status, item.why_en_status) for item in items} == {("ready", "ready")}
            assert all(item.why_zh_hant is None for item in items)

            tw = await latest_edition(database, "tw_equity", "zh-hant", today=date(2026, 9, 29))
            assert [item.headline for item in tw.items] == ["T 臺灣標題", "U 臺灣標題"]
            linked = await database.scalar(
                select(NewsroomArticle).where(NewsroomArticle.url == "https://example.com/linked")
            )
            assert linked is not None and linked.title == "Existing"
            assert linked.event_id == tw.items[0].event_id
            taken = await database.scalar(
                select(NewsroomArticle).where(NewsroomArticle.url == "https://example.com/taken")
            )
            assert taken is not None and taken.event_id == seeded["taken_event_id"]

            created = await database.scalar(
                select(NewsroomArticle).where(NewsroomArticle.url == "https://example.com/a")
            )
            assert created is not None
            assert (created.body, created.body_status, created.fetch_status) == (
                None,
                "purged",
                "done",
            )
            assert (created.embed_status, created.triage_status, created.relevant) == (
                "idle",
                "done",
                True,
            )
            assert created.title == "Source headline 1"

            us = await database.scalar(
                select(NewsroomEdition).where(NewsroomEdition.market_code == "us_equity")
            )
            assert us is not None and us.selection_mode == "editor"
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", BEFORE_IMPORT)
    engine, sessions = await _sessions(database_url)
    try:
        async with sessions() as database:
            assert (
                await database.scalar(
                    select(NewsroomEdition.id).where(NewsroomEdition.selection_mode == "legacy")
                )
                is None
            )
            assert (
                await database.scalar(
                    select(NewsroomEvent.id).where(NewsroomEvent.created_by == "legacy")
                )
                is None
            )
            remaining = (
                await database.execute(select(NewsroomArticle.url, NewsroomArticle.event_id))
            ).all()
            assert sorted(remaining) == sorted(
                [
                    ("https://example.com/linked", None),
                    ("https://example.com/taken", seeded["taken_event_id"]),
                ]
            )
    finally:
        await engine.dispose()


async def test_legacy_tables_are_dropped_once_their_history_moved(database_url: str) -> None:
    _seed_legacy(database_url)
    runs = _seed_legacy_runs(database_url)
    _alembic(database_url, "upgrade", DROP_LEGACY)

    state = _legacy_state(database_url, runs)
    assert not set(LEGACY_TABLES) & state["tables"]
    assert state["statuses"] == {
        "pending_job": "cancelled",
        "pending_function": "cancelled",
        "finished_job": "succeeded",
        "finished_function": "succeeded",
    }
    engine, sessions = await _sessions(database_url)
    try:
        async with sessions() as database:
            edition = await latest_edition(database, "global", "zh-hant", today=date(2026, 9, 29))
            assert [item.headline for item in edition.items] == ["A 臺灣標題", "B 臺灣發佈頭條"]
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", IMPORT)
    restored = _legacy_state(database_url, runs)
    assert set(LEGACY_TABLES) <= restored["tables"]
    assert restored["statuses"]["pending_function"] == "cancelled"
    _alembic(database_url, "downgrade", BEFORE_IMPORT)
    _alembic(database_url, "upgrade", DROP_LEGACY)
