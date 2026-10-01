"""Database-backed tests for /api/admin/newsroom.

Other workstreams' service functions are replaced with recording fakes, so
these tests pin down what the admin API validates, which service it calls with
what, and the plain field updates (remove, hide, order) it writes itself.
"""

import os
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_health import readiness

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import LOCAL_SESSION_SECRET, Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.models import Base
from daily_insights_api.core.security import hash_token
from daily_insights_api.modules.identity.api import AuthContext
from daily_insights_api.modules.identity.auth import get_auth_context
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.modules.newsroom import (
    clock,
    events_service,
    publishing,
    sources_service,
)
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEditionItem,
    NewsroomEditLog,
    NewsroomEvent,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.models import NewsroomEdition as Edition
from daily_insights_api.web.app import create_app

pytestmark = pytest.mark.integration

CSRF_TOKEN = "csrf-token"
DAY = date(2026, 10, 1)
BASE = "/api/admin/newsroom"


@pytest_asyncio.fixture
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required for integration tests")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def admin(database: async_sessionmaker[AsyncSession]) -> User:
    async with database() as session:
        user = User(
            email="newsroom-admin@example.com",
            display_name="Newsroom Admin",
            password_hash="unused",
            must_change_password=False,
            system_role=SystemRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        await session.commit()
        return user


@pytest_asyncio.fixture
async def client(
    database: async_sessionmaker[AsyncSession], admin: User
) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(environment="test"), readiness(True), session_factory=database)
    session = Session(csrf_token_hash=hash_token(CSRF_TOKEN, LOCAL_SESSION_SECRET))

    async def authenticated() -> AuthContext:
        return AuthContext(user=admin, session=session, organization_id=None)

    app.dependency_overrides[get_auth_context] = authenticated
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-CSRF-Token": CSRF_TOKEN},
    ) as http:
        yield http


@dataclass
class Calls:
    """Arguments each faked service function received, by function name."""

    log: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def record(self, name: str, **kwargs: Any) -> None:
        self.log.setdefault(name, []).append(kwargs)

    def of(self, name: str) -> list[dict[str, Any]]:
        return self.log.get(name, [])


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Replace every cross-workstream service with a fake that records its call."""
    recorded = Calls()
    new_item_id = uuid.uuid4()
    new_event_id = uuid.uuid4()
    new_article_id = uuid.uuid4()

    async def create_source(
        database: AsyncSession, data: sources_service.SourceInput, *, user_id: uuid.UUID
    ) -> uuid.UUID:
        recorded.record("create_source", data=data, user_id=user_id)
        source = NewsroomSource(
            key=data.key,
            name=data.name,
            kind=data.kind,
            url=data.url,
            hostname=data.hostname,
            markets=list(data.markets),
            trust_tier=data.trust_tier,
            weight=data.weight,
            poll_interval_minutes=data.poll_interval_minutes,
            enabled=data.enabled,
        )
        database.add(source)
        await database.flush()
        return source.id

    async def update_source(
        database: AsyncSession,
        source_id: uuid.UUID,
        changes: dict[str, object],
        *,
        user_id: uuid.UUID,
    ) -> None:
        recorded.record("update_source", source_id=source_id, changes=changes, user_id=user_id)
        source = await database.get(NewsroomSource, source_id)
        assert source is not None
        for name, value in changes.items():
            setattr(source, name, list(value) if isinstance(value, tuple) else value)

    def recorder(name: str, result: Any = None) -> Any:
        async def fake(database: AsyncSession, *args: Any, **kwargs: Any) -> Any:
            del database
            recorded.record(name, args=args, **kwargs)
            return result

        return fake

    monkeypatch.setattr(sources_service, "create_source", create_source)
    monkeypatch.setattr(sources_service, "update_source", update_source)
    monkeypatch.setattr(
        sources_service, "submit_manual_url", recorder("submit_manual_url", new_article_id)
    )
    monkeypatch.setattr(sources_service, "set_manual_body", recorder("set_manual_body"))
    monkeypatch.setattr(events_service, "merge_events", recorder("merge_events"))
    monkeypatch.setattr(events_service, "split_event", recorder("split_event", new_event_id))
    monkeypatch.setattr(publishing, "publish_edition", recorder("publish_edition"))
    monkeypatch.setattr(
        publishing, "add_event_to_edition", recorder("add_event_to_edition", new_item_id)
    )
    monkeypatch.setattr(publishing, "apply_event_edit", recorder("apply_event_edit"))
    monkeypatch.setattr(publishing, "apply_why_edit", recorder("apply_why_edit"))
    monkeypatch.setattr(publishing, "reanalyze_event", recorder("reanalyze_event"))
    recorded.record("ids", item=new_item_id, event=new_event_id, article=new_article_id)
    return recorded


# --- Seed helpers -------------------------------------------------------------


async def _source(
    database: async_sessionmaker[AsyncSession], key: str, **values: Any
) -> NewsroomSource:
    async with database() as session:
        source = NewsroomSource(
            key=key,
            name=values.pop("name", key.title()),
            kind=values.pop("kind", "rss"),
            hostname=values.pop("hostname", f"{key}.example"),
            markets=values.pop("markets", ["global"]),
            **values,
        )
        session.add(source)
        await session.commit()
        return source


async def _event(
    database: async_sessionmaker[AsyncSession], title: str, **values: Any
) -> NewsroomEvent:
    async with database() as session:
        event = NewsroomEvent(
            edition_date=values.pop("edition_date", DAY), working_title=title, **values
        )
        session.add(event)
        await session.commit()
        return event


async def _article(
    database: async_sessionmaker[AsyncSession],
    source: NewsroomSource,
    event: NewsroomEvent | None,
    *,
    scores: dict[str, int] | None = None,
    **values: Any,
) -> NewsroomArticle:
    async with database() as session:
        url = f"https://{source.hostname}/{uuid.uuid4()}"
        article = NewsroomArticle(
            source_id=source.id,
            url=url,
            url_hash=uuid.uuid4().hex * 2,
            title=values.pop("title", f"Story from {source.key}"),
            edition_date=values.pop("edition_date", DAY),
            event_id=event.id if event is not None else None,
            relevant=values.pop("relevant", event is not None),
            market_scores=scores or {"global": 50, "tw_equity": 10, "us_equity": 10},
            triage_status=values.pop("triage_status", "done"),
            **values,
        )
        session.add(article)
        await session.commit()
        return article


async def _edition(
    database: async_sessionmaker[AsyncSession], market_code: str = "global", **values: Any
) -> Edition:
    async with database() as session:
        edition_date = values.pop("edition_date", DAY)
        edition = Edition(
            edition_date=edition_date,
            market_code=market_code,
            auto_publish_at=clock.auto_publish_at(edition_date),
            late_fill_deadline=clock.late_fill_deadline(edition_date),
            **values,
        )
        session.add(edition)
        await session.commit()
        return edition


async def _item(
    database: async_sessionmaker[AsyncSession],
    edition: Edition,
    event: NewsroomEvent,
    rank: int,
    **values: Any,
) -> NewsroomEditionItem:
    async with database() as session:
        item = NewsroomEditionItem(edition_id=edition.id, event_id=event.id, rank=rank, **values)
        session.add(item)
        await session.commit()
        return item


async def _published(database: async_sessionmaker[AsyncSession], market: str) -> Edition:
    return await _edition(database, market, status="published", published_at=datetime.now(UTC))


async def _logs(database: async_sessionmaker[AsyncSession]) -> Sequence[NewsroomEditLog]:
    async with database() as session:
        return (
            await session.scalars(select(NewsroomEditLog).order_by(NewsroomEditLog.created_at))
        ).all()


async def _get_item(
    database: async_sessionmaker[AsyncSession], item_id: uuid.UUID
) -> NewsroomEditionItem:
    async with database() as session:
        item = await session.get(NewsroomEditionItem, item_id)
        assert item is not None
        return item


# --- Sources ------------------------------------------------------------------


async def test_sources_report_health_and_recent_article_counts(
    database: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    now = datetime.now(UTC)
    healthy = await _source(database, "healthy", last_success_at=now)
    await _source(
        database,
        "degraded",
        consecutive_failures=2,
        last_success_at=now - timedelta(hours=1),
        last_error_code="http_503",
    )
    await _source(database, "unhealthy", consecutive_failures=5, last_success_at=None)
    await _source(database, "fresh")
    await _source(database, "off", enabled=False)
    await _article(database, healthy, None)
    await _article(database, healthy, None, first_seen_at=now - timedelta(days=8))

    response = await client.get(f"{BASE}/sources")

    assert response.status_code == 200
    sources = {source["key"]: source for source in response.json()["sources"]}
    assert {key: source["health"] for key, source in sources.items()} == {
        "healthy": "healthy",
        "degraded": "degraded",
        "unhealthy": "unhealthy",
        "fresh": "pending",
        "off": "disabled",
    }
    assert sources["healthy"]["articles_7d"] == 1
    assert sources["degraded"]["last_error_code"] == "http_503"
    # Enabled sources lead.
    assert list(sources)[-1] == "off"


async def test_create_source_goes_through_the_ingestion_service(
    client: AsyncClient, calls: Calls, admin: User
) -> None:
    response = await client.post(
        f"{BASE}/sources",
        json={
            "key": "reuters-markets",
            "name": "Reuters Markets",
            "kind": "rss",
            "url": "https://reuters.example/markets.rss",
            "hostname": "Reuters.Example",
            "markets": ["global", "us_equity"],
            "trust_tier": 3,
            "weight": 1.5,
            "language_filter": ["en"],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["key"] == "reuters-markets"
    assert body["hostname"] == "reuters.example"
    assert body["health"] == "pending"
    [call] = calls.of("create_source")
    data = call["data"]
    assert isinstance(data, sources_service.SourceInput)
    assert data.markets == ("global", "us_equity")
    assert data.language_filter == ("en",)
    assert data.trust_tier == 3 and data.weight == 1.5
    assert call["user_id"] == admin.id

    duplicate = await client.post(
        f"{BASE}/sources",
        json={"key": "reuters-markets", "name": "Again", "kind": "rss", "hostname": "r.example"},
    )
    assert duplicate.status_code == 409
    assert len(calls.of("create_source")) == 1


async def test_source_service_errors_map_to_http_statuses(
    client: AsyncClient, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    del calls
    payload = {"key": "x", "name": "X", "kind": "rss", "hostname": "x.example"}

    async def invalid(*args: Any, **kwargs: Any) -> uuid.UUID:
        raise ValueError("feed URL is not reachable")

    monkeypatch.setattr(sources_service, "create_source", invalid)
    response = await client.post(f"{BASE}/sources", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"] == "feed URL is not reachable"

    async def stub(*args: Any, **kwargs: Any) -> uuid.UUID:
        raise NotImplementedError

    monkeypatch.setattr(sources_service, "create_source", stub)
    response = await client.post(f"{BASE}/sources", json=payload)
    assert response.status_code == 501


async def test_update_source_passes_only_the_changed_fields(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls
) -> None:
    source = await _source(database, "bloomberg", link_pattern="/news/")
    await _source(database, "taken")

    response = await client.patch(
        f"{BASE}/sources/{source.id}",
        json={"enabled": False, "markets": ["tw_equity"], "link_pattern": None},
    )

    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    assert response.json()["health"] == "disabled"
    assert response.json()["link_pattern"] is None
    [call] = calls.of("update_source")
    assert call["changes"] == {"enabled": False, "markets": ("tw_equity",), "link_pattern": None}

    cleared = await client.patch(f"{BASE}/sources/{source.id}", json={"name": None})
    assert cleared.status_code == 422
    conflict = await client.patch(f"{BASE}/sources/{source.id}", json={"key": "taken"})
    assert conflict.status_code == 409
    missing = await client.patch(f"{BASE}/sources/{uuid.uuid4()}", json={"enabled": True})
    assert missing.status_code == 404
    assert len(calls.of("update_source")) == 1


async def test_manual_source_kind_cannot_change(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls
) -> None:
    manual = await _source(database, "manual", kind="manual")

    response = await client.patch(f"{BASE}/sources/{manual.id}", json={"kind": "rss"})

    assert response.status_code == 409
    assert calls.of("update_source") == []


# --- Editions -----------------------------------------------------------------


async def test_edition_day_summarises_each_market(
    database: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    source = await _source(database, "wire")
    ready = await _event(database, "Ready", analysis_status="ready")
    failed = await _event(database, "Failed", analysis_status="failed")
    blocked = await _event(database, "Blocked", analysis_status="needs_body")
    dropped = await _event(database, "Dropped", analysis_status="ready")
    edition = await _edition(
        database, "global", selection_mode="fallback", ignored_pending_triage=3
    )
    await _item(database, edition, ready, 1, why_status="ready")
    await _item(database, edition, failed, 2)
    await _item(database, edition, blocked, 3)
    await _item(database, edition, dropped, 4, removed_at=datetime.now(UTC))
    await _article(database, source, None, triage_status="pending")
    await _article(database, source, None, triage_status="idle")
    await _article(database, source, None, triage_status="failed")
    await _article(database, source, None, triage_status="idle", embed_status="failed")

    response = await client.get(f"{BASE}/editions", params={"date": DAY.isoformat()})

    assert response.status_code == 200
    body = response.json()
    assert body["edition_date"] == DAY.isoformat()
    assert body["is_today"] is (DAY == clock.taipei_today())
    assert body["untriaged_articles"] == 2
    assert body["triage_failed_articles"] == 1
    markets = {market["market_code"]: market for market in body["markets"]}
    assert list(markets) == ["global", "tw_equity", "us_equity"]
    assert markets["global"]["edition"]["selection_mode"] == "fallback"
    assert markets["global"]["edition"]["ignored_pending_triage"] == 3
    assert markets["global"]["counts"] == {
        "active": 3,
        "removed": 1,
        "hidden": 0,
        "abandoned": 0,
        "ready": 1,
        "analysis_failed": 1,
        "needs_body": 1,
    }
    assert markets["tw_equity"]["edition"] is None
    assert markets["tw_equity"]["counts"]["active"] == 0


async def test_edition_day_defaults_to_taipei_today(client: AsyncClient) -> None:
    response = await client.get(f"{BASE}/editions")

    assert response.status_code == 200
    assert response.json()["edition_date"] == clock.taipei_today().isoformat()
    assert response.json()["is_today"] is True


async def test_edition_detail_lists_items_and_scored_candidates(
    database: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    wire = await _source(database, "wire", trust_tier=3, weight=2.0)
    blog = await _source(database, "blog", trust_tier=1, weight=0.5)
    paper = await _source(database, "paper")
    placed = await _event(
        database,
        "Placed",
        analysis_status="ready",
        headline_zh_hant="央行升息",
        related_symbols=[
            {"symbol": "^TWII", "kind": "index", "label": "加權指數"},
            # Not a dashboard kind: dropped rather than failing the page.
            {"symbol": "2330", "kind": "stock", "label": "台積電"},
        ],
    )
    second = await _event(database, "Second")
    edition = await _edition(database, "global")
    first_item = await _item(database, edition, placed, 2, stars=5, why_status="ready")
    second_item = await _item(database, edition, second, 1, stars=4)
    await _article(database, wire, placed, body_status="ok")
    await _article(database, blog, placed)
    await _article(database, wire, second)

    # Candidate scores: max(score * weight) + 5 per extra distinct source (≤ 20).
    lone = await _event(database, "Lone wire story")
    await _article(database, wire, lone, scores={"global": 40, "tw_equity": 0, "us_equity": 0})
    crowded = await _event(database, "Widely reported")
    for source in (wire, blog, paper):
        await _article(
            database, source, crowded, scores={"global": 30, "tw_equity": 0, "us_equity": 0}
        )
    await _event(database, "No relevant articles")
    merged = await _event(database, "Merged away", status="merged", merged_into_id=lone.id)
    await _article(database, wire, merged)
    other_day = await _event(database, "Yesterday", edition_date=DAY - timedelta(days=1))
    await _article(database, wire, other_day, edition_date=DAY - timedelta(days=1))

    response = await client.get(f"{BASE}/editions/{DAY.isoformat()}/global")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["edition"]["id"] == str(edition.id)
    assert [item["id"] for item in body["items"]] == [str(second_item.id), str(first_item.id)]
    shown = body["items"][1]
    assert shown["stars"] == 5
    assert shown["event"]["headline_zh_hant"] == "央行升息"
    assert shown["event"]["related_symbols"] == [
        {"symbol": "^TWII", "kind": "index", "label": "加權指數"}
    ]
    assert shown["event"]["article_count"] == 2
    assert shown["event"]["source_count"] == 2
    assert shown["event"]["body_ok_count"] == 1
    # The most trusted source's article leads the card.
    assert [link["source_name"] for link in shown["event"]["articles"]] == ["Wire", "Blog"]
    assert [
        (candidate["event"]["working_title"], candidate["score"])
        for candidate in body["candidates"]
    ] == [("Lone wire story", 80.0), ("Widely reported", 70.0)]
    assert body["candidates"][1]["event"]["body_ok_count"] == 0


async def test_edition_detail_without_an_edition_still_lists_candidates(
    database: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    wire = await _source(database, "wire")
    event = await _event(database, "Story")
    await _article(database, wire, event, scores={"global": 0, "tw_equity": 60, "us_equity": 0})

    response = await client.get(f"{BASE}/editions/{DAY.isoformat()}/tw_equity")

    assert response.status_code == 200
    assert response.json()["edition"] is None
    assert response.json()["items"] == []
    assert [c["score"] for c in response.json()["candidates"]] == [60.0]


async def test_publish_one_market(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls, admin: User
) -> None:
    draft = await _edition(database, "global")
    published = await _published(database, "tw_equity")

    response = await client.post(f"{BASE}/editions/{draft.id}/publish")

    assert response.status_code == 204
    assert calls.of("publish_edition") == [{"args": (draft.id,), "user_id": admin.id}]
    again = await client.post(f"{BASE}/editions/{published.id}/publish")
    assert again.status_code == 409
    missing = await client.post(f"{BASE}/editions/{uuid.uuid4()}/publish")
    assert missing.status_code == 404
    assert len(calls.of("publish_edition")) == 1


async def test_publish_all_markets_skips_published_ones(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls
) -> None:
    global_draft = await _edition(database, "global")
    await _published(database, "tw_equity")
    us_draft = await _edition(database, "us_equity")
    await _edition(database, "global", edition_date=DAY + timedelta(days=1))

    response = await client.post(f"{BASE}/editions/publish", json={"edition_date": str(DAY)})

    assert response.status_code == 204
    assert [call["args"][0] for call in calls.of("publish_edition")] == [
        global_draft.id,
        us_draft.id,
    ]


async def test_add_candidate_event_to_edition(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls, admin: User
) -> None:
    edition = await _edition(database, "global")
    candidate = await _event(database, "Candidate")
    placed = await _event(database, "Placed")
    await _item(database, edition, placed, 1)
    merged = await _event(database, "Merged", status="merged", merged_into_id=candidate.id)
    yesterday = await _event(database, "Yesterday", edition_date=DAY - timedelta(days=1))

    response = await client.post(
        f"{BASE}/editions/{edition.id}/items", json={"event_id": str(candidate.id)}
    )

    assert response.status_code == 201
    assert response.json() == {"item_id": str(calls.of("ids")[0]["item"])}
    assert calls.of("add_event_to_edition") == [
        {"args": (edition.id, candidate.id), "user_id": admin.id}
    ]
    for event_id, expected in [
        (placed.id, 409),
        (merged.id, 409),
        (yesterday.id, 409),
        (uuid.uuid4(), 404),
    ]:
        rejected = await client.post(
            f"{BASE}/editions/{edition.id}/items", json={"event_id": str(event_id)}
        )
        assert rejected.status_code == expected
    assert len(calls.of("add_event_to_edition")) == 1


async def test_reorder_renumbers_ranks_and_logs_the_change(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, admin: User
) -> None:
    edition = await _edition(database, "global")
    events = [await _event(database, f"Event {index}") for index in range(3)]
    items = [
        await _item(database, edition, event, rank)
        for event, rank in zip(events, (1, 2, 5), strict=True)
    ]
    order = [items[2].id, items[0].id, items[1].id]

    response = await client.put(
        f"{BASE}/editions/{edition.id}/order", json={"item_ids": [str(i) for i in order]}
    )

    assert response.status_code == 204
    assert [(await _get_item(database, item_id)).rank for item_id in order] == [1, 2, 3]
    [entry] = await _logs(database)
    assert (entry.entity_type, entry.entity_id, entry.action) == ("edition", edition.id, "reorder")
    assert entry.user_id == admin.id
    assert entry.before == {"order": [str(item.id) for item in items]}
    assert entry.after == {"order": [str(item_id) for item_id in order]}

    same = await client.put(
        f"{BASE}/editions/{edition.id}/order", json={"item_ids": [str(i) for i in order]}
    )
    assert same.status_code == 204
    assert len(await _logs(database)) == 1

    partial = await client.put(
        f"{BASE}/editions/{edition.id}/order", json={"item_ids": [str(order[0])]}
    )
    assert partial.status_code == 422
    stranger = await client.put(
        f"{BASE}/editions/{edition.id}/order",
        json={"item_ids": [str(i) for i in order[:2]] + [str(uuid.uuid4())]},
    )
    assert stranger.status_code == 422


async def test_remove_and_restore_a_draft_item(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, admin: User
) -> None:
    edition = await _edition(database, "global")
    event = await _event(database, "Story")
    item = await _item(database, edition, event, 1)

    removed = await client.post(f"{BASE}/items/{item.id}/remove")
    repeated = await client.post(f"{BASE}/items/{item.id}/remove")

    assert removed.status_code == 204 and repeated.status_code == 204
    assert (await _get_item(database, item.id)).removed_at is not None
    restored = await client.post(f"{BASE}/items/{item.id}/restore")
    assert restored.status_code == 204
    assert (await _get_item(database, item.id)).removed_at is None
    logs = await _logs(database)
    assert [(entry.entity_type, entry.action) for entry in logs] == [
        ("item", "remove"),
        ("item", "restore"),
    ]
    assert logs[0].before == {"removed": False}
    assert logs[0].after == {
        "removed": True,
        "edition_id": str(edition.id),
        "event_id": str(event.id),
    }
    assert {entry.user_id for entry in logs} == {admin.id}
    hide = await client.post(f"{BASE}/items/{item.id}/hide")
    assert hide.status_code == 409
    missing = await client.post(f"{BASE}/items/{uuid.uuid4()}/remove")
    assert missing.status_code == 404


async def test_hide_and_unhide_a_published_item(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, admin: User
) -> None:
    edition = await _published(database, "us_equity")
    item = await _item(database, edition, await _event(database, "Story"), 1)

    hidden = await client.post(f"{BASE}/items/{item.id}/hide")

    assert hidden.status_code == 204
    row = await _get_item(database, item.id)
    assert row.hidden_at is not None and row.hidden_by_user_id == admin.id
    remove = await client.post(f"{BASE}/items/{item.id}/remove")
    assert remove.status_code == 409
    shown = await client.post(f"{BASE}/items/{item.id}/unhide")
    assert shown.status_code == 204
    row = await _get_item(database, item.id)
    assert row.hidden_at is None and row.hidden_by_user_id is None
    assert [entry.action for entry in await _logs(database)] == ["hide", "unhide"]


async def test_edit_why_requires_a_ready_why(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls, admin: User
) -> None:
    edition = await _edition(database, "global")
    ready = await _item(database, edition, await _event(database, "Ready"), 1, why_status="ready")
    pending = await _item(database, edition, await _event(database, "Pending"), 2)

    response = await client.put(
        f"{BASE}/items/{ready.id}/why", json={"why": "  利率走勢牽動資金  "}
    )

    assert response.status_code == 204
    assert calls.of("apply_why_edit") == [
        {"args": (ready.id, "利率走勢牽動資金"), "user_id": admin.id}
    ]
    blocked = await client.put(f"{BASE}/items/{pending.id}/why", json={"why": "x"})
    assert blocked.status_code == 409
    assert len(calls.of("apply_why_edit")) == 1


# --- Events -------------------------------------------------------------------


async def test_edit_event_passes_zh_hant_changes(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls, admin: User
) -> None:
    event = await _event(database, "Story", analysis_status="ready")
    pending = await _event(database, "Pending", analysis_status="pending")

    response = await client.patch(
        f"{BASE}/events/{event.id}",
        json={
            "summary": "央行宣布升息半碼。",
            "related_symbols": [{"symbol": "^TWII", "kind": "index", "label": "加權指數"}],
        },
    )

    assert response.status_code == 204
    assert calls.of("apply_event_edit") == [
        {
            "args": (event.id,),
            "headline": None,
            "summary": "央行宣布升息半碼。",
            "related_symbols": [{"symbol": "^TWII", "kind": "index", "label": "加權指數"}],
            "user_id": admin.id,
        }
    ]
    nothing = await client.patch(f"{BASE}/events/{event.id}", json={})
    assert nothing.status_code == 422
    not_ready = await client.patch(f"{BASE}/events/{pending.id}", json={"headline": "x"})
    assert not_ready.status_code == 409
    missing = await client.patch(f"{BASE}/events/{uuid.uuid4()}", json={"headline": "x"})
    assert missing.status_code == 404


async def test_reanalyze_event(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls, admin: User
) -> None:
    event = await _event(database, "Story", analysis_status="failed")
    merged = await _event(database, "Merged", status="merged", merged_into_id=event.id)

    response = await client.post(f"{BASE}/events/{event.id}/reanalyze")

    assert response.status_code == 204
    assert calls.of("reanalyze_event") == [{"args": (event.id,), "user_id": admin.id}]
    rejected = await client.post(f"{BASE}/events/{merged.id}/reanalyze")
    assert rejected.status_code == 409


async def test_merge_events_validates_before_calling_the_service(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls, admin: User
) -> None:
    target = await _event(database, "Target")
    source = await _event(database, "Source")
    other_day = await _event(database, "Other day", edition_date=DAY + timedelta(days=1))

    response = await client.post(
        f"{BASE}/events/merge",
        json={"target_id": str(target.id), "source_ids": [str(source.id)]},
    )

    assert response.status_code == 204
    assert calls.of("merge_events") == [
        {"args": (), "target_id": target.id, "source_ids": [source.id], "user_id": admin.id}
    ]
    for payload, expected in [
        ({"target_id": str(target.id), "source_ids": [str(target.id)]}, 422),
        ({"target_id": str(target.id), "source_ids": [str(other_day.id)]}, 409),
        ({"target_id": str(target.id), "source_ids": [str(uuid.uuid4())]}, 404),
    ]:
        rejected = await client.post(f"{BASE}/events/merge", json=payload)
        assert rejected.status_code == expected
    assert len(calls.of("merge_events")) == 1


async def test_split_event_moves_some_articles_out(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls, admin: User
) -> None:
    wire = await _source(database, "wire")
    event = await _event(database, "Two stories")
    keep = await _article(database, wire, event)
    move = await _article(database, wire, event)
    stranger = await _article(database, wire, await _event(database, "Other"))

    response = await client.post(
        f"{BASE}/events/{event.id}/split", json={"article_ids": [str(move.id)]}
    )

    assert response.status_code == 201
    assert response.json() == {"event_id": str(calls.of("ids")[0]["event"])}
    assert calls.of("split_event") == [
        {"args": (), "event_id": event.id, "article_ids": [move.id], "user_id": admin.id}
    ]
    everything = await client.post(
        f"{BASE}/events/{event.id}/split",
        json={"article_ids": [str(keep.id), str(move.id)]},
    )
    assert everything.status_code == 422
    foreign = await client.post(
        f"{BASE}/events/{event.id}/split", json={"article_ids": [str(stranger.id)]}
    )
    assert foreign.status_code == 422
    assert len(calls.of("split_event")) == 1


async def test_event_detail_shows_articles_bodies_and_placements(
    database: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    wire = await _source(database, "wire", trust_tier=3)
    blog = await _source(database, "blog", trust_tier=1)
    event = await _event(database, "Story")
    full = await _article(
        database,
        wire,
        event,
        body="全文" * 3_000,
        body_status="ok",
        body_source="fetch",
        fetch_status="done",
    )
    missing = await _article(
        database, blog, event, body_status="unavailable", fetch_status="failed"
    )
    global_edition = await _edition(database, "global")
    us_edition = await _published(database, "us_equity")
    await _item(database, global_edition, event, 1, removed_at=datetime.now(UTC))
    await _item(database, us_edition, event, 3)

    response = await client.get(f"{BASE}/events/{event.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["event"]["id"] == str(event.id)
    assert [article["id"] for article in body["articles"]] == [str(full.id), str(missing.id)]
    assert body["articles"][0]["body_length"] == 6_000
    assert len(body["articles"][0]["body_preview"]) == 4_000
    assert body["articles"][0]["source_key"] == "wire"
    assert body["articles"][1]["body_preview"] is None
    assert body["articles"][1]["body_length"] == 0
    assert body["articles"][1]["body_status"] == "unavailable"
    assert [
        (placement["market_code"], placement["edition_status"], placement["removed"])
        for placement in body["placements"]
    ] == [("global", "draft", True), ("us_equity", "published", False)]
    missing_event = await client.get(f"{BASE}/events/{uuid.uuid4()}")
    assert missing_event.status_code == 404


# --- Articles -----------------------------------------------------------------


async def test_paste_full_text(
    database: async_sessionmaker[AsyncSession], client: AsyncClient, calls: Calls, admin: User
) -> None:
    wire = await _source(database, "wire")
    article = await _article(database, wire, await _event(database, "Story"))

    response = await client.put(
        f"{BASE}/articles/{article.id}/body", json={"body": "  第一段。\n\n第二段。  "}
    )

    assert response.status_code == 204
    assert calls.of("set_manual_body") == [
        {"args": (article.id, "第一段。\n\n第二段。"), "user_id": admin.id}
    ]
    missing = await client.put(f"{BASE}/articles/{uuid.uuid4()}/body", json={"body": "x"})
    assert missing.status_code == 404


async def test_submit_manual_url(client: AsyncClient, calls: Calls, admin: User) -> None:
    response = await client.post(
        f"{BASE}/articles/manual",
        json={"url": "https://news.example/story", "edition_date": DAY.isoformat()},
    )

    assert response.status_code == 201
    assert response.json() == {"article_id": str(calls.of("ids")[0]["article"])}
    assert calls.of("submit_manual_url") == [
        {"args": ("https://news.example/story",), "edition_date": DAY, "user_id": admin.id}
    ]


class FakeEventServiceError(Exception):
    """Stand-in for workstream ②'s ``events_service.EventServiceError``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("event_not_found", 404),
        ("event_not_open", 409),
        ("edition_date_mismatch", 409),
        ("invalid_merge_sources", 422),
        ("empty_split", 422),
        ("article_not_in_event", 422),
        ("split_takes_every_article", 422),
        ("something_new", 422),
    ],
)
async def test_event_service_errors_map_by_code(
    database: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    calls: Calls,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    expected: int,
) -> None:
    del calls
    monkeypatch.setattr(events_service, "EventServiceError", FakeEventServiceError, raising=False)

    async def rejected(*args: Any, **kwargs: Any) -> None:
        raise FakeEventServiceError(code)

    monkeypatch.setattr(events_service, "merge_events", rejected)
    monkeypatch.setattr(events_service, "split_event", rejected)
    wire = await _source(database, "wire")
    target = await _event(database, "Target")
    source = await _event(database, "Source")
    keep = await _article(database, wire, target)
    await _article(database, wire, target)

    merged = await client.post(
        f"{BASE}/events/merge",
        json={"target_id": str(target.id), "source_ids": [str(source.id)]},
    )
    split = await client.post(
        f"{BASE}/events/{target.id}/split", json={"article_ids": [str(keep.id)]}
    )

    assert (merged.status_code, merged.json()["detail"]) == (expected, code)
    assert (split.status_code, split.json()["detail"]) == (expected, code)
