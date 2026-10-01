import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole
from daily_insights_api.core.models import Base
from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.newsroom import public_api
from daily_insights_api.modules.newsroom.clock import taipei_today
from daily_insights_api.modules.newsroom.contracts import MarketCode
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEvent,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.public_api import latest_edition, zh_hant_digest
from daily_insights_api.web.app import create_app
from daily_insights_api.web.dependencies import get_database_session

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 1)
YESTERDAY = TODAY - timedelta(days=1)
PUBLISHED_AT = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)


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


class _Newsroom:
    """Builds published editions whose items are visible unless told otherwise."""

    def __init__(self, database: AsyncSession) -> None:
        self.database = database
        self.sources: dict[str, NewsroomSource] = {}
        self.editions: dict[tuple[date, str], NewsroomEdition] = {}

    async def source(self, key: str, *, kind: str = "rss", trust_tier: int = 2) -> NewsroomSource:
        if key not in self.sources:
            source = NewsroomSource(
                key=key,
                name=key.title(),
                kind=kind,
                hostname=f"{key}.example",
                markets=["global"],
                trust_tier=trust_tier,
            )
            self.database.add(source)
            await self.database.flush()
            self.sources[key] = source
        return self.sources[key]

    async def edition(
        self, edition_date: date, market: str = "global", *, status: str = "published"
    ) -> NewsroomEdition:
        key = (edition_date, market)
        if key not in self.editions:
            edition = NewsroomEdition(
                edition_date=edition_date,
                market_code=market,
                status=status,
                selection_mode="editor",
                auto_publish_at=PUBLISHED_AT,
                late_fill_deadline=PUBLISHED_AT + timedelta(hours=3),
                published_at=PUBLISHED_AT if status == "published" else None,
            )
            self.database.add(edition)
            await self.database.flush()
            self.editions[key] = edition
        return self.editions[key]

    async def event(self, edition_date: date, title: str, **overrides: Any) -> NewsroomEvent:
        values: dict[str, Any] = {
            "edition_date": edition_date,
            "working_title": title,
            "headline_zh_hant": f"{title} 繁",
            "summary_zh_hant": f"{title} 繁摘要",
            "headline_zh_hans": f"{title} 简",
            "summary_zh_hans": f"{title} 简摘要",
            "headline_en": f"{title} en",
            "summary_en": f"{title} en summary",
            "analysis_status": "ready",
            "en_status": "ready",
            "related_symbols": [],
        }
        values.update(overrides)
        event = NewsroomEvent(**values)
        self.database.add(event)
        await self.database.flush()
        return event

    async def item(
        self,
        edition_date: date,
        event: NewsroomEvent,
        *,
        market: str = "global",
        edition_status: str = "published",
        rank: int = 1,
        **overrides: Any,
    ) -> NewsroomEditionItem:
        edition = await self.edition(edition_date, market, status=edition_status)
        values: dict[str, Any] = {
            "edition_id": edition.id,
            "event_id": event.id,
            "rank": rank,
            "stars": 4,
            "why_zh_hant": f"{event.working_title} {market} 為何重要",
            "why_zh_hans": f"{event.working_title} {market} 为何重要",
            "why_en": f"{event.working_title} {market} why",
            "why_status": "ready",
            "why_en_status": "ready",
        }
        values.update(overrides)
        item = NewsroomEditionItem(**values)
        self.database.add(item)
        await self.database.flush()
        return item

    async def article(
        self,
        event: NewsroomEvent,
        source: NewsroomSource,
        url: str,
        *,
        published_at: datetime | None = None,
    ) -> NewsroomArticle:
        article = NewsroomArticle(
            source_id=source.id,
            url=url,
            url_hash=uuid.uuid4().hex * 2,
            title=url,
            edition_date=event.edition_date,
            event_id=event.id,
            published_at=published_at,
        )
        self.database.add(article)
        await self.database.flush()
        return article

    async def sign_english(self, event: NewsroomEvent) -> None:
        """Stamp the digest translation (③) writes over the event's current zh-hant."""
        await self.database.flush()
        await self.database.refresh(event)
        digests = await public_api._current_digests(self.database, [event])
        event.en_source_digest = digests[event.id]
        await self.database.flush()


async def _latest(
    database: AsyncSession,
    locale: public_api.Locale = "zh-hant",
    market: MarketCode = "global",
    linkable: frozenset[str] = frozenset(),
) -> public_api.NewsroomEditionResponse:
    return await latest_edition(database, market, locale, today=TODAY, linkable_markets=linkable)


async def test_returns_todays_visible_items_with_sources_and_symbols(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        newsroom = _Newsroom(database)
        event = await newsroom.event(
            TODAY,
            "Fed",
            related_symbols=[
                {"symbol": "^TWII", "kind": "index", "label": "加權指數"},
                {"symbol": "^HSI", "kind": "index", "label": "恒生指數"},
                {"symbol": "2330.TW", "kind": "equity", "label": "台積電"},
                {"symbol": 7, "kind": "index"},
            ],
        )
        second = await newsroom.event(TODAY, "Oil")
        await newsroom.item(TODAY, second, rank=2, stars=None)
        item = await newsroom.item(TODAY, event, rank=1)
        low = await newsroom.source("blog", trust_tier=1)
        high = await newsroom.source("wire", trust_tier=3)
        manual = await newsroom.source("manual", kind="manual")
        await newsroom.article(event, low, "https://blog.example/a")
        await newsroom.article(
            event, high, "https://wire.example/a", published_at=PUBLISHED_AT - timedelta(hours=1)
        )
        await newsroom.article(event, high, "https://wire.example/b", published_at=PUBLISHED_AT)
        await newsroom.article(event, manual, "https://pasted.example/story")
        await newsroom.article(event, low, "javascript:alert(1)")
        await database.commit()

        response = await _latest(database, linkable=frozenset({"tw_equity"}))

    assert response.edition_date == TODAY
    assert response.is_today is True
    assert response.published_at == PUBLISHED_AT
    assert [entry.headline for entry in response.items] == ["Fed 繁", "Oil 繁"]
    first = response.items[0]
    assert first.id == item.id
    assert first.event_id == event.id
    assert (first.summary, first.why, first.stars) == ("Fed 繁摘要", "Fed global 為何重要", 4)
    assert response.items[1].stars is None
    assert [(source.name, source.url) for source in first.sources] == [
        ("Wire", "https://wire.example/b"),
        ("Wire", "https://wire.example/a"),
        ("pasted.example", "https://pasted.example/story"),
        ("Blog", "https://blog.example/a"),
    ]
    assert [
        (symbol.symbol, symbol.label, symbol.market_code) for symbol in first.related_symbols
    ] == [
        ("^TWII", "加權指數", "tw_equity"),
        # Charted on the Hong Kong page, which this viewer cannot open.
        ("^HSI", "恒生指數", None),
        ("2330.TW", "台積電", None),
    ]


@pytest.mark.parametrize(
    ("event_overrides", "item_overrides"),
    [
        ({}, {"removed_at": PUBLISHED_AT}),
        ({}, {"hidden_at": PUBLISHED_AT}),
        ({}, {"abandoned_at": PUBLISHED_AT}),
        ({"analysis_status": "pending"}, {}),
        ({"analysis_status": "needs_body"}, {}),
        ({}, {"why_status": "pending"}),
        ({}, {"why_status": "failed"}),
    ],
    ids=[
        "removed",
        "hidden",
        "abandoned",
        "analysis-pending",
        "analysis-needs-body",
        "why-pending",
        "why-failed",
    ],
)
async def test_hidden_items_fall_back_to_the_latest_visible_edition(
    newsroom_database: async_sessionmaker[AsyncSession],
    event_overrides: dict[str, Any],
    item_overrides: dict[str, Any],
) -> None:
    async with newsroom_database() as database:
        newsroom = _Newsroom(database)
        today_event = await newsroom.event(TODAY, "Today", **event_overrides)
        await newsroom.item(TODAY, today_event, **item_overrides)
        older = await newsroom.event(YESTERDAY - timedelta(days=1), "Older")
        await newsroom.item(YESTERDAY - timedelta(days=1), older)
        yesterday = await newsroom.event(YESTERDAY, "Yesterday")
        await newsroom.item(YESTERDAY, yesterday)
        await database.commit()

        response = await _latest(database)

    assert response.edition_date == YESTERDAY
    assert response.is_today is False
    assert [item.headline for item in response.items] == ["Yesterday 繁"]


async def test_only_visible_items_of_the_chosen_edition_are_listed(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        newsroom = _Newsroom(database)
        shown = await newsroom.event(TODAY, "Shown")
        await newsroom.item(TODAY, shown, rank=2)
        hidden = await newsroom.event(TODAY, "Hidden")
        await newsroom.item(TODAY, hidden, rank=1, hidden_at=PUBLISHED_AT)
        await database.commit()

        response = await _latest(database)

    assert [item.headline for item in response.items] == ["Shown 繁"]
    assert response.items[0].rank == 2


async def test_drafts_future_editions_and_other_markets_are_ignored(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        newsroom = _Newsroom(database)
        draft = await newsroom.event(TODAY, "Draft")
        await newsroom.item(TODAY, draft, edition_status="draft")
        future = await newsroom.event(TODAY + timedelta(days=1), "Future")
        await newsroom.item(TODAY + timedelta(days=1), future)
        taiwan = await newsroom.event(TODAY, "Taiwan")
        await newsroom.item(TODAY, taiwan, market="tw_equity")
        await database.commit()

        global_response = await _latest(database)
        taiwan_response = await _latest(database, market="tw_equity")
        us_response = await _latest(database, market="us_equity")

    assert global_response.edition_id is None
    assert global_response.items == []
    assert [item.headline for item in taiwan_response.items] == ["Taiwan 繁"]
    assert us_response.items == []


async def test_empty_result_when_nothing_is_published(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        response = await _latest(database, locale="en", market="us_equity")

    assert response.model_dump() == {
        "market_code": "us_equity",
        "locale": "en",
        "edition_id": None,
        "edition_date": None,
        "is_today": False,
        "published_at": None,
        "items": [],
    }


async def test_simplified_chinese_reads_the_converted_text(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        newsroom = _Newsroom(database)
        event = await newsroom.event(TODAY, "Fed")
        await newsroom.item(TODAY, event)
        unconverted = await newsroom.event(TODAY, "Raw", headline_zh_hans=None)
        await newsroom.item(TODAY, unconverted, rank=2)
        await database.commit()

        response = await _latest(database, locale="zh-hans")

    assert [(item.headline, item.summary, item.why) for item in response.items] == [
        ("Fed 简", "Fed 简摘要", "Fed global 为何重要")
    ]


async def test_english_needs_a_fresh_translation(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        newsroom = _Newsroom(database)
        fresh = await newsroom.event(TODAY, "Fresh")
        await newsroom.item(TODAY, fresh, rank=1)
        # The same event also runs in the Taiwan edition; its "why" is part
        # of the text the English was translated from.
        await newsroom.item(TODAY, fresh, market="tw_equity", rank=1)
        await newsroom.sign_english(fresh)

        pending = await newsroom.event(TODAY, "Pending", en_status="pending")
        await newsroom.item(TODAY, pending, rank=2)
        await newsroom.sign_english(pending)

        stale = await newsroom.event(TODAY, "Stale")
        await newsroom.item(TODAY, stale, rank=3)
        await newsroom.sign_english(stale)
        stale.summary_zh_hant = "編輯改過的摘要"

        why_pending = await newsroom.event(TODAY, "WhyPending")
        await newsroom.item(TODAY, why_pending, rank=4, why_en_status="pending")
        await newsroom.sign_english(why_pending)

        unsigned = await newsroom.event(TODAY, "Unsigned")
        await newsroom.item(TODAY, unsigned, rank=5)

        stale_why = await newsroom.event(TODAY, "StaleWhy")
        stale_item = await newsroom.item(TODAY, stale_why, rank=6)
        await newsroom.sign_english(stale_why)
        stale_item.why_zh_hant = "改過的為何重要"
        await database.commit()

        english = await _latest(database, locale="en")
        chinese = await _latest(database, locale="zh-hant")

    assert [item.headline for item in english.items] == ["Fresh en"]
    assert english.items[0].why == "Fresh global why"
    assert len(chinese.items) == 6


async def test_english_turns_stale_when_another_market_changes_its_why(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        newsroom = _Newsroom(database)
        event = await newsroom.event(TODAY, "Shared")
        await newsroom.item(TODAY, event)
        taiwan = await newsroom.item(TODAY, event, market="tw_equity")
        await newsroom.sign_english(event)
        await database.commit()
        assert (await _latest(database, locale="en")).items

        taiwan.why_zh_hant = "台股版改寫"
        await database.commit()
        assert (await _latest(database, locale="en")).items == []

        # Hiding the Taiwan item removes its "why" from the digest instead.
        taiwan.why_zh_hant = f"{event.working_title} tw_equity 為何重要"
        taiwan.hidden_at = PUBLISHED_AT
        await database.commit()
        assert (await _latest(database, locale="en")).items == []


async def test_english_falls_back_past_editions_whose_translation_is_stale(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        newsroom = _Newsroom(database)
        for offset in range(10):
            day = TODAY - timedelta(days=offset)
            event = await newsroom.event(day, f"Day{offset}")
            await newsroom.item(day, event)
            await newsroom.sign_english(event)
            # Every edition newer than the tenth has stale English.
            if offset < 9:
                event.headline_zh_hant = "改過"
        await database.commit()

        response = await _latest(database, locale="en")

    assert response.edition_date == TODAY - timedelta(days=9)
    assert [item.headline for item in response.items] == ["Day9 en"]


def test_digest_matches_the_agreed_serialization() -> None:
    serialized = '{"headline":"標題","summary":null,"whys":{"a":null,"b":"二"}}'
    assert zh_hant_digest("標題", None, {"b": "二", "a": None}) == (
        hashlib.sha256(serialized.encode()).hexdigest()
    )


def _member(role: str, organization_id: uuid.UUID | None) -> AuthContext:
    return cast(
        AuthContext,
        SimpleNamespace(user=SimpleNamespace(system_role=role), organization_id=organization_id),
    )


async def test_endpoint_serves_the_latest_edition_without_caching(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    today = taipei_today()
    async with newsroom_database() as database:
        newsroom = _Newsroom(database)
        event = await newsroom.event(
            today,
            "Fed",
            related_symbols=[{"symbol": "^GSPC", "kind": "index", "label": "標普500"}],
        )
        await newsroom.item(today, event, market="us_equity")
        await newsroom.sign_english(event)
        await database.commit()

    app = create_app(Settings(environment="test"), readiness_checker=_ready)

    async def auth() -> AuthContext:
        return _member(SystemRole.ADMIN, None)

    async def session() -> AsyncIterator[AsyncSession]:
        async with newsroom_database() as database:
            yield database

    app.dependency_overrides[require_password_changed] = auth
    app.dependency_overrides[get_database_session] = session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/newsroom/editions/latest?market=us_equity&locale=en")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["edition_date"] == today.isoformat()
    assert body["is_today"] is True
    assert body["items"][0]["headline"] == "Fed en"
    assert body["items"][0]["related_symbols"] == [
        {"symbol": "^GSPC", "kind": "index", "label": "^GSPC", "market_code": "us_equity"}
    ]


async def _ready() -> bool:
    return True
