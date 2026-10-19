import math
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.models import Base
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.newsroom import clock, quality
from daily_insights_api.modules.newsroom.models import (
    EMBEDDING_DIMENSIONS,
    NewsroomArticle,
    NewsroomDedupeDecision,
    NewsroomEdition,
    NewsroomEditionItem,
    NewsroomEditLog,
    NewsroomEditorRating,
    NewsroomEvent,
    NewsroomSource,
)
from daily_insights_api.scripts import run_newsroom_quality

pytestmark = pytest.mark.integration

DAY = date(2026, 10, 8)
MOMENT = datetime(2026, 10, 8, 1, tzinfo=UTC)


def vector(degrees: float) -> list[float]:
    radians = math.radians(degrees)
    return [math.cos(radians), math.sin(radians)] + [0.0] * (EMBEDDING_DIMENSIONS - 2)


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


async def _seed(database: AsyncSession) -> None:
    user = User(
        email="editor@example.com",
        display_name="Editor",
        password_hash="unused",
        must_change_password=False,
        system_role=SystemRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    source = NewsroomSource(key="wire", name="Wire", kind="rss", hostname="w.example", markets=[])
    database.add_all([user, source])
    await database.flush()

    def article(event: NewsroomEvent | None, angle: float, **values: object) -> NewsroomArticle:
        return NewsroomArticle(
            source_id=source.id,
            url=f"https://w.example/{uuid.uuid4()}",
            url_hash=uuid.uuid4().hex * 2,
            title="t",
            edition_date=event.edition_date if event else DAY,
            embedding=vector(angle),
            event_id=event.id if event else None,
            **values,
        )

    def edition(day: date) -> NewsroomEdition:
        return NewsroomEdition(
            edition_date=day,
            market_code="tw_equity",
            status="published",
            published_at=MOMENT,
            auto_publish_at=clock.auto_publish_at(day),
            late_fill_deadline=clock.late_fill_deadline(day),
        )

    yesterday = DAY - timedelta(days=1)
    revenue_before = NewsroomEvent(edition_date=yesterday, working_title="鴻海9月營收")
    revenue_again = NewsroomEvent(edition_date=DAY, working_title="鴻海9月營收創新高")
    other = NewsroomEvent(edition_date=DAY, working_title="央行理監事會")
    dropped = NewsroomEvent(edition_date=DAY, working_title="被移除的事件")
    added = NewsroomEvent(edition_date=DAY, working_title="管理員加入")
    database.add_all([revenue_before, revenue_again, other, dropped, added])
    earlier, today = edition(yesterday), edition(DAY)
    database.add_all([earlier, today])
    await database.flush()
    window_opens = clock.window(DAY)[0]
    database.add_all(
        [
            article(revenue_before, 0, relevant=True),
            # Same story a day later: near-identical embedding, published again.
            article(
                revenue_again, 1, relevant=True, published_at=window_opens - timedelta(hours=9)
            ),
            article(other, 60, relevant=True, published_at=window_opens),
            article(other, 62, relevant=True),
            article(dropped, 120, relevant=True),
            article(added, 170, relevant=True),
            article(None, 90, relevant=False),
        ]
    )
    items = [
        NewsroomEditionItem(edition_id=earlier.id, event_id=revenue_before.id, rank=1, stars=5),
        NewsroomEditionItem(edition_id=today.id, event_id=revenue_again.id, rank=1, stars=4),
        NewsroomEditionItem(edition_id=today.id, event_id=other.id, rank=2, stars=4),
        NewsroomEditionItem(
            edition_id=today.id, event_id=dropped.id, rank=3, stars=3, removed_at=MOMENT
        ),
        NewsroomEditionItem(
            edition_id=today.id, event_id=added.id, rank=4, origin="manual", stars=None
        ),
    ]
    database.add_all(items)
    await database.flush()
    database.add_all(
        [
            NewsroomEditLog(
                entity_type="event", entity_id=other.id, action="edit", user_id=user.id
            ),
            NewsroomEditLog(
                entity_type="item", entity_id=items[1].id, action="edit_why", user_id=user.id
            ),
            NewsroomDedupeDecision(
                edition_date=DAY,
                scope="published",
                event_id=revenue_again.id,
                event_title="鴻海9月營收創新高",
                matched_event_id=revenue_before.id,
                matched_title="鴻海9月營收",
                title_similarity=0.9,
                threshold=0.85,
                shared_figure=False,
                new_facts=True,
                outcome="follow_up",
            ),
        ]
    )

    def rating(
        event: NewsroomEvent, position: int, stars: int, picked: bool, at: datetime
    ) -> NewsroomEditorRating:
        return NewsroomEditorRating(
            edition_date=DAY,
            market_code="tw_equity",
            event_id=event.id,
            event_title=event.working_title,
            position=position,
            score=80.0,
            stars=stars,
            picked=picked,
            follow_up=False,
            prompt_version="v",
            created_at=at,
        )

    # An earlier assembly run is superseded by the rebuild an hour later.
    database.add_all(
        [
            rating(other, 1, 2, False, MOMENT - timedelta(hours=1)),
            rating(revenue_again, 1, 4, True, MOMENT),
            rating(other, 2, 4, False, MOMENT),
            rating(dropped, 3, 3, True, MOMENT),
        ]
    )
    await database.commit()


async def test_quality_report_measures_edits_repeats_and_screens(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    async with newsroom_database() as database:
        await _seed(database)
        report = await quality.quality_report(database, DAY, DAY, sample=5)

    [day] = report.collection
    assert (day.articles, day.relevant, day.published_before_window) == (6, 5, 1)
    assert (day.events, day.multi_article_events, day.published_events) == (4, 1, 3)
    [edition] = report.editions
    assert (edition.model_items, edition.removed_model_items, edition.manual_items) == (3, 1, 1)
    assert edition.edited_items == 2
    assert edition.replacement_rate == pytest.approx(2 / 3)
    assert (edition.stars, edition.removed_by_stars) == ({4: 2, 3: 1}, {3: 1})
    assert report.visible_items == 3
    assert report.same_edition_repeats == []
    [repeat] = report.cross_day_repeats
    assert (repeat.title, repeat.earlier_title, repeat.earlier_date) == (
        "鴻海9月營收創新高",
        "鴻海9月營收",
        DAY - timedelta(days=1),
    )
    assert report.dedupe_decisions == {"published": {"follow_up": 1}}
    assert report.editor_ratings == {4: (2, 1), 3: (1, 1)}
    [unpicked] = report.unpicked_high_stars
    assert (unpicked.title, unpicked.position, unpicked.stars) == ("央行理監事會", 2, 4)
    # Only items with an analysed headline can be checked against their sources.
    assert report.fact_check_sample == []

    rendered = run_newsroom_quality.render(report)
    assert "換稿率 67%" in rendered
    assert "跨日重複 1 則" in rendered
    assert "published 提議 1 組 follow_up 1" in rendered
    assert "4 星以上未入選 1 則" in rendered


def test_quality_script_defaults_to_the_last_seven_days() -> None:
    options = run_newsroom_quality.parse_args(["--end", "2026-10-15"])
    assert (options.start, options.end, options.sample) == (
        date(2026, 10, 9),
        date(2026, 10, 15),
        10,
    )
    with pytest.raises(SystemExit):
        run_newsroom_quality.parse_args(["--start", "2026-10-16", "--end", "2026-10-15"])
