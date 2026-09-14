import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pytest
import pytest_asyncio
from conftest import remigrate_database
from fastapi import Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_health import readiness

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.security import hash_password
from daily_insights_api.modules.audit.models import AuditEvent
from daily_insights_api.modules.data_management.models import DataManagementRun
from daily_insights_api.modules.identity.api import (
    AuthContext,
    require_csrf,
    require_password_changed,
)
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.modules.news.editions import GLOBAL_SPEC
from daily_insights_api.modules.news.models import (
    NewsCandidate,
    NewsEdition,
    NewsItem,
    NewsPresentation,
)
from daily_insights_api.modules.news.router import _latest_response
from daily_insights_api.modules.news.service import TAIPEI
from daily_insights_api.web.app import create_app

pytestmark = pytest.mark.integration


async def test_failed_or_empty_new_revision_falls_back_without_resurrecting_hidden_stories(
    news_admin_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(news_admin_database)
    today = datetime.now(TAIPEI).date()
    previous, _, _ = await _seed_global_edition(news_admin_database, today - timedelta(days=1))
    async with news_admin_database.begin() as database:
        current = NewsEdition(
            edition_date=today,
            market_code="global",
            revision=1,
            input_digest="c" * 64,
            derivation_version="test",
            prompt_version="test",
            status="partial",
        )
        database.add(current)
        await database.flush()
        hidden = _item(current.id, 1)
        database.add(hidden)
        await database.flush()
        for locale in ("en", "zh-hant", "zh-hans"):
            database.add(
                NewsPresentation(
                    item_id=hidden.id,
                    locale=locale,
                    headline="Current story",
                    summary="Validated summary",
                )
            )
        hidden_id = hidden.id
        # Zero-story complete versions must not replace the readable fallback.
        database.add(
            NewsEdition(
                edition_date=today,
                market_code="global",
                revision=2,
                input_digest="d" * 64,
                derivation_version="test",
                prompt_version="test",
                status="complete",
            )
        )
        database.add(
            NewsEdition(
                edition_date=today,
                market_code="global",
                revision=3,
                input_digest="e" * 64,
                derivation_version="test",
                prompt_version="test",
                status="unavailable",
            )
        )
    async with _client(news_admin_database, user) as client:
        response = await client.post(f"/api/admin/news/items/{hidden_id}/hide")
        assert response.status_code == 200
    async with news_admin_database() as database:
        for locale in ("en", "zh-hant", "zh-hans"):
            result = await _latest_response(database, Response(), cast(Any, locale), GLOBAL_SPEC)
            assert result.edition_id == previous
            assert len(result.items) == 1 and result.items[0].event_key == "story-2"
            assert result.caveat is None


@pytest_asyncio.fixture
async def news_admin_database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.environ.get("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    remigrate_database(database_url)
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as database:
        database.add(
            User(
                email="news-admin@example.com",
                display_name="News Admin",
                password_hash=hash_password("Password123!", "test-pepper"),
                must_change_password=False,
                system_role=SystemRole.ADMIN,
                status=UserStatus.ACTIVE,
            )
        )
    try:
        yield factory
    finally:
        await engine.dispose()


async def _admin(factory: async_sessionmaker[AsyncSession]) -> User:
    async with factory() as database:
        user = await database.scalar(select(User).where(User.email == "news-admin@example.com"))
    assert user is not None
    return user


def _client(
    factory: async_sessionmaker[AsyncSession], user: User, *, enabled: bool = True
) -> AsyncClient:
    app = create_app(
        Settings(
            environment="test",
            daily_news_enabled=enabled,
            news_model_api_key="key" if enabled else None,
        ),
        readiness(True),
        session_factory=factory,
    )

    async def authenticated() -> AuthContext:
        return AuthContext(user=user, session=Session(), organization_id=None)

    app.dependency_overrides[require_password_changed] = authenticated
    app.dependency_overrides[require_csrf] = authenticated
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _item(edition_id: uuid.UUID, rank: int) -> NewsItem:
    return NewsItem(
        edition_id=edition_id,
        rank=rank,
        topic="markets",
        source_name=f"Source {rank}",
        source_hostname=f"source{rank}.example",
        source_url=f"https://source{rank}.example/story-{rank}",
        source_headline=f"Story {rank}",
        importance=4,
        content_digest=str(rank) * 64,
        numeric_facts=[],
        market="global",
        event_key=f"story-{rank}",
    )


def _candidate(edition_id: uuid.UUID, index: int, **overrides: object) -> NewsCandidate:
    values: dict[str, object] = dict(
        edition_id=edition_id,
        candidate_id=str(index) * 64,
        source_name=f"Source {index}",
        hostname=f"source{index}.example",
        url=f"https://source{index}.example/story-{index}",
        headline=f"Story {index}",
        seen_at=datetime(2026, 9, 8, 0, index, tzinfo=UTC),
        stage="reviewed",
    )
    values.update(overrides)
    return NewsCandidate(**values)


async def _seed_global_edition(
    factory: async_sessionmaker[AsyncSession], edition_date: date
) -> tuple[uuid.UUID, list[uuid.UUID], list[uuid.UUID]]:
    """A global edition with two published stories and four other candidates."""
    async with factory.begin() as database:
        edition = NewsEdition(
            edition_date=edition_date,
            market_code="global",
            revision=1,
            input_digest="d" * 64,
            derivation_version="test",
            prompt_version="selection-v7:test+summary-v3",
            status="partial",
        )
        database.add(edition)
        await database.flush()
        items = [_item(edition.id, 1), _item(edition.id, 2)]
        database.add_all(items)
        await database.flush()
        for item in items:
            for locale in ("zh-hant", "zh-hans", "en"):
                database.add(
                    NewsPresentation(
                        item_id=item.id,
                        locale=locale,
                        headline=f"{locale} headline {item.rank}",
                        summary=f"{locale} summary {item.rank}",
                    )
                )
        candidates = [
            _candidate(edition.id, 1, stage="published", item_id=items[0].id, ai_rank=2),
            _candidate(edition.id, 2, stage="published", item_id=items[1].id, ai_rank=1),
            _candidate(edition.id, 3, stage="dropped", drop_reason="off_market", ai_rank=4),
            _candidate(edition.id, 4, stage="dropped", drop_reason="summary_failed", ai_rank=3),
            _candidate(edition.id, 5),
            _candidate(edition.id, 6, stage="discovered", seen_at=None),
        ]
        database.add_all(candidates)
        await database.flush()
        return (
            edition.id,
            [item.id for item in items],
            [candidate.id for candidate in candidates],
        )


async def test_admin_editions_list_every_market_with_counts_and_ordered_candidates(
    news_admin_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(news_admin_database)
    today = datetime.now(TAIPEI).date()
    edition_id, item_ids, candidate_ids = await _seed_global_edition(news_admin_database, today)
    async with _client(news_admin_database, user) as client:
        default = await client.get("/api/admin/news/editions")
        explicit = await client.get(f"/api/admin/news/editions?date={today.isoformat()}")
        empty = await client.get("/api/admin/news/editions?date=2020-01-01")
        invalid = await client.get("/api/admin/news/editions?date=not-a-date")

    assert default.status_code == 200 and explicit.status_code == 200
    assert default.json() == explicit.json()
    body = default.json()
    assert body["edition_date"] == today.isoformat()
    assert [entry["market_code"] for entry in body["editions"]] == [
        "global",
        "tw_equity",
        "us_equity",
    ]
    entry = body["editions"][0]
    assert entry["edition"]["id"] == str(edition_id)
    assert entry["edition"]["revision"] == 1 and entry["edition"]["status"] == "partial"
    assert entry["edition"]["target_items"] == GLOBAL_SPEC.target_items
    assert entry["edition"]["counts"] == {
        "discovered": 1,
        "fetch_failed": 0,
        "unused": 0,
        "reviewed": 1,
        "dropped": 2,
        "published": 2,
        "hidden": 0,
    }
    assert [item["id"] for item in entry["items"]] == [str(item_id) for item_id in item_ids]
    first = entry["items"][0]
    assert first["headline"] == "zh-hant headline 1" and first["origin"] == "model"
    assert first["hidden"] is False and first["candidate_id"] == str(candidate_ids[0])
    # Published by item rank, dropped by model rank, reviewed, then the rest.
    assert [candidate["id"] for candidate in entry["candidates"]] == [
        str(candidate_ids[index]) for index in (0, 1, 3, 2, 4, 5)
    ]
    assert entry["candidates"][3]["drop_reason"] == "off_market"
    for other in body["editions"][1:]:
        assert other == {
            "market_code": other["market_code"],
            "edition": None,
            "items": [],
            "candidates": [],
        }
    assert empty.json()["editions"][0]["edition"] is None
    assert invalid.status_code == 422


async def test_hide_and_unhide_are_idempotent_audited_and_hide_from_readers(
    news_admin_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(news_admin_database)
    today = datetime.now(TAIPEI).date()
    _, item_ids, _ = await _seed_global_edition(news_admin_database, today)
    hidden_id = item_ids[0]
    async with _client(news_admin_database, user) as client:
        hidden = await client.post(f"/api/admin/news/items/{hidden_id}/hide")
        again = await client.post(f"/api/admin/news/items/{hidden_id}/hide")
        listed = await client.get("/api/admin/news/editions")
        unknown = await client.post(f"/api/admin/news/items/{uuid.uuid4()}/hide")
        malformed = await client.post("/api/admin/news/items/not-a-uuid/unhide")

    assert hidden.status_code == 200 and again.status_code == 200
    assert hidden.json()["hidden"] is True and hidden.json()["hidden_at"] is not None
    assert again.json()["hidden_at"] == hidden.json()["hidden_at"]
    assert listed.json()["editions"][0]["edition"]["counts"]["hidden"] == 1
    assert unknown.status_code == 404 and malformed.status_code == 404

    async with news_admin_database() as database:
        reader = await _latest_response(database, Response(), "zh-hant", GLOBAL_SPEC)
        assert [item.id for item in reader.items] == [item_ids[1]]
        actions = list(
            await database.scalars(
                select(AuditEvent.action).where(AuditEvent.target_id == str(hidden_id))
            )
        )
    assert actions == ["news.item_hidden"]

    async with _client(news_admin_database, user) as client:
        shown = await client.post(f"/api/admin/news/items/{hidden_id}/unhide")
    assert shown.status_code == 200 and shown.json()["hidden"] is False
    async with news_admin_database() as database:
        reader = await _latest_response(database, Response(), "en", GLOBAL_SPEC)
        assert [item.id for item in reader.items] == item_ids
        actions = list(
            await database.scalars(
                select(AuditEvent.action).where(AuditEvent.target_id == str(hidden_id))
            )
        )
    assert actions == ["news.item_hidden", "news.item_unhidden"]


async def test_publish_request_validates_and_queues_one_news_publish_run(
    news_admin_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(news_admin_database)
    today = datetime.now(TAIPEI).date()
    edition_id, _, candidate_ids = await _seed_global_edition(news_admin_database, today)
    chosen = [str(candidate_ids[2]), str(candidate_ids[4])]
    async with _client(news_admin_database, user) as client:
        unknown_edition = await client.post(
            "/api/admin/news/candidates/publish",
            json={"edition_id": str(uuid.uuid4()), "candidate_ids": chosen},
        )
        foreign_candidate = await client.post(
            "/api/admin/news/candidates/publish",
            json={"edition_id": str(edition_id), "candidate_ids": [str(uuid.uuid4())]},
        )
        already_published = await client.post(
            "/api/admin/news/candidates/publish",
            json={"edition_id": str(edition_id), "candidate_ids": [str(candidate_ids[0])]},
        )
        duplicate_ids = await client.post(
            "/api/admin/news/candidates/publish",
            json={"edition_id": str(edition_id), "candidate_ids": [chosen[0], chosen[0]]},
        )
        accepted = await client.post(
            "/api/admin/news/candidates/publish",
            json={"edition_id": str(edition_id), "candidate_ids": chosen},
        )
        conflict = await client.post(
            "/api/admin/news/candidates/publish",
            json={"edition_id": str(edition_id), "candidate_ids": [str(candidate_ids[3])]},
        )
        listed = await client.get("/api/admin/data-management/runs?operation_group=news")
        editions = await client.get("/api/admin/news/editions")

    assert unknown_edition.status_code == 404
    assert foreign_candidate.status_code == 422
    assert already_published.status_code == 422
    assert duplicate_ids.status_code == 422
    assert accepted.status_code == 202, accepted.text
    run = accepted.json()
    assert run["operation"] == "news_publish" and run["market_code"] is None
    assert run["status"] == "pending" and run["requested_by_user_id"] == str(user.id)
    assert run["edition_date"] == today.isoformat()
    assert conflict.status_code == 409
    assert [item["operation"] for item in listed.json()["items"]] == ["news_publish"]
    candidates = {
        candidate["id"]: candidate for candidate in editions.json()["editions"][0]["candidates"]
    }
    for candidate_id in chosen:
        assert candidates[candidate_id]["publish_run_id"] == run["id"]
        assert candidates[candidate_id]["publish_requested_at"] is not None
        assert candidates[candidate_id]["publish_error"] is None
    assert candidates[str(candidate_ids[3])]["publish_run_id"] is None

    async with news_admin_database() as database:
        stored = await database.get(DataManagementRun, uuid.UUID(run["id"]))
        assert stored is not None
        assert stored.payload == {"edition_id": str(edition_id), "candidate_ids": chosen}
        requested = list(
            await database.scalars(
                select(AuditEvent).where(AuditEvent.action == "news.candidate_publish_requested")
            )
        )
    assert len(requested) == 1 and requested[0].target_id == str(edition_id)
    assert cast(dict[str, Any], requested[0].after)["candidate_ids"] == chosen


async def test_publish_request_is_refused_for_superseded_editions_and_when_disabled(
    news_admin_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(news_admin_database)
    yesterday = datetime.now(TAIPEI).date() - timedelta(days=1)
    edition_id, _, candidate_ids = await _seed_global_edition(news_admin_database, yesterday)
    payload = {"edition_id": str(edition_id), "candidate_ids": [str(candidate_ids[4])]}
    async with _client(news_admin_database, user, enabled=False) as client:
        disabled = await client.post("/api/admin/news/candidates/publish", json=payload)
    assert disabled.status_code == 503

    async with news_admin_database.begin() as database:
        database.add(
            NewsEdition(
                edition_date=yesterday,
                market_code="global",
                revision=2,
                input_digest="e" * 64,
                derivation_version="test",
                prompt_version="test",
                status="unavailable",
            )
        )
    async with _client(news_admin_database, user) as client:
        superseded = await client.post("/api/admin/news/candidates/publish", json=payload)
        listed = await client.get(f"/api/admin/news/editions?date={yesterday.isoformat()}")
    assert superseded.status_code == 409
    assert superseded.json()["detail"] == "edition superseded"
    # The admin view always shows the latest revision.
    assert listed.json()["editions"][0]["edition"]["revision"] == 2
    assert listed.json()["editions"][0]["candidates"] == []
    async with news_admin_database() as database:
        runs = list(await database.scalars(select(DataManagementRun)))
    assert runs == []
