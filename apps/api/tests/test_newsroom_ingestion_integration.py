"""Polling, the FETCH stage, health notices, purge, and the admin source service."""

import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings, get_settings
from daily_insights_api.core.models import Base
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.newsroom import clock, queue, sources_service
from daily_insights_api.modules.newsroom.ingestion import safe_http
from daily_insights_api.modules.newsroom.ingestion.deps import IngestionDeps
from daily_insights_api.modules.newsroom.ingestion.feeds import url_hash
from daily_insights_api.modules.newsroom.ingestion.fetching import make_fetch_handler
from daily_insights_api.modules.newsroom.ingestion.polling import (
    HostThrottle,
    claim_due_sources,
    run_poll_cycle,
)
from daily_insights_api.modules.newsroom.ingestion.purge import purge_old_bodies
from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEditLog,
    NewsroomEvent,
    NewsroomSource,
)
from daily_insights_api.modules.newsroom.notifier import LogNotifier
from daily_insights_api.modules.newsroom.queue import claim, run_claimed
from daily_insights_api.modules.newsroom.sources_service import (
    SourceConflictError,
    SourceInput,
    SourceValidationError,
)

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures" / "newsroom"
HOST = "www.example-news.com"
# 11:00 Taipei on 2026-09-02 belongs to the 2026-09-03 edition.
NOW = datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
Handler = Callable[[httpx.Request], httpx.Response]


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


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, environment="test", **overrides)


async def public_resolver(host: str, port: int) -> list[str]:
    del host, port
    return ["93.184.216.34"]


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def deps(handler: Handler, now: Clock | None = None) -> IngestionDeps:
    def client_factory(allowed: frozenset[str], timeout: float) -> httpx.AsyncClient:
        del allowed, timeout
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return IngestionDeps(
        client_factory=client_factory, resolver=public_resolver, clock=now or Clock(NOW)
    )


def serving(feed: bytes | Handler, robots: str = "User-agent: *\nAllow: /\n") -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if callable(feed):
            return feed(request)
        return httpx.Response(200, content=feed)

    return handler


def rss(*items: tuple[str, str, datetime | None]) -> bytes:
    body = "".join(
        f"<item><title>{title}</title><link>https://{HOST}/news/{slug}</link>"
        + (f"<pubDate>{seen:%a, %d %b %Y %H:%M:%S} +0000</pubDate>" if seen else "")
        + "</item>"
        for title, slug, seen in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'.encode()


async def add_source(
    session_factory: async_sessionmaker[AsyncSession], **overrides: Any
) -> uuid.UUID:
    values: dict[str, Any] = {
        "key": f"source-{uuid.uuid4().hex[:8]}",
        "name": "Example News",
        "kind": "rss",
        "url": f"https://{HOST}/feed.xml",
        "hostname": HOST,
        "link_pattern": r"^https://www\.example-news\.com/.+$",
        "markets": ["global"],
        "poll_interval_minutes": 30,
    }
    values.update(overrides)
    async with session_factory() as database:
        source = NewsroomSource(**values)
        database.add(source)
        await database.commit()
        return source.id


async def add_user(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as database:
        user = User(
            email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Admin",
            password_hash="not-a-real-hash",
            must_change_password=False,
        )
        database.add(user)
        await database.commit()
        return user.id


async def poll(
    session_factory: async_sessionmaker[AsyncSession],
    handler: Handler,
    *,
    now: Clock | None = None,
    notifier: LogNotifier | None = None,
    **setting_overrides: Any,
) -> Any:
    return await run_poll_cycle(
        session_factory,
        settings(**setting_overrides),
        notifier or LogNotifier(),
        deps(handler, now),
        HostThrottle(),
    )


async def articles(session_factory: async_sessionmaker[AsyncSession]) -> list[NewsroomArticle]:
    async with session_factory() as database:
        return list(
            (await database.scalars(select(NewsroomArticle).order_by(NewsroomArticle.title))).all()
        )


async def source_row(
    session_factory: async_sessionmaker[AsyncSession], source_id: uuid.UUID
) -> NewsroomSource:
    async with session_factory() as database:
        row = await database.get(NewsroomSource, source_id)
        assert row is not None
        return row


# --- polling ------------------------------------------------------------------


async def test_poll_stores_articles_with_insert_time_queue_state(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await add_source(newsroom_database, kind="rss_full", full_text_in_feed=True)

    [outcome] = await poll(newsroom_database, serving((FIXTURES / "rss_full_ok.xml").read_bytes()))

    assert (outcome.status, outcome.inserted) == ("ok", 3)
    rows = {row.title: row for row in await articles(newsroom_database)}
    full = rows["Full article"]
    assert full.body is not None and full.body.startswith("This paragraph is long")
    assert (full.body_status, full.body_source, full.fetch_status) == ("ok", "feed", "done")
    for title in ("Teaser article", "No encoded body"):
        assert (rows[title].body_status, rows[title].fetch_status) == ("pending", "pending")
        assert rows[title].body is None
    for row in rows.values():
        assert row.embed_status == "pending"
        assert row.triage_status == "idle"
        assert row.first_seen_at == NOW
        assert row.edition_date == date(2026, 9, 3)
        assert row.url_hash == url_hash(row.url)
    source = await source_row(newsroom_database, source_id)
    assert source.last_success_at == NOW and source.last_polled_at == NOW
    assert source.consecutive_failures == 0
    assert source.next_poll_at == NOW + timedelta(minutes=30)


async def test_poll_skips_known_urls_same_edition_titles_and_stale_items(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    await add_source(newsroom_database)
    feed = rss(
        ("Fed holds rates", "fed", NOW - timedelta(hours=1)),
        ("Fed holds rates", "fed-copy", NOW - timedelta(hours=1)),
        ("Old story", "old", NOW - timedelta(hours=30)),
        ("Undated story", "undated", None),
    )
    await poll(newsroom_database, serving(feed))
    assert [row.title for row in await articles(newsroom_database)] == [
        "Fed holds rates",
        "Undated story",
    ]

    # Due again an hour later: known URLs and the same title in the same
    # edition are skipped, a new story is stored.
    later = Clock(NOW + timedelta(hours=1))
    async with newsroom_database() as database:
        await database.execute(
            NewsroomSource.__table__.update().values(next_poll_at=None)  # type: ignore[attr-defined]
        )
        await database.commit()
    second = rss(
        ("Fed holds rates", "fed", None),
        ("Fed holds rates", "fed-again", None),
        ("Oil jumps", "oil", None),
    )
    [outcome] = await poll(newsroom_database, serving(second), now=later)
    assert outcome.inserted == 1
    assert sorted(row.title for row in await articles(newsroom_database)) == [
        "Fed holds rates",
        "Oil jumps",
        "Undated story",
    ]


async def test_edition_window_turns_over_at_eight_taipei(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    await add_source(newsroom_database)
    before_cutoff = Clock(datetime(2026, 9, 2, 23, 59, 59, tzinfo=UTC))  # 07:59:59 Taipei
    at_cutoff = Clock(datetime(2026, 9, 3, 0, 0, tzinfo=UTC))  # 08:00 Taipei
    await poll(newsroom_database, serving(rss(("Fed holds rates", "a", None))), now=before_cutoff)
    async with newsroom_database() as database:
        await database.execute(
            NewsroomSource.__table__.update().values(next_poll_at=None)  # type: ignore[attr-defined]
        )
        await database.commit()
    # The same headline in the next edition window is a new article.
    await poll(newsroom_database, serving(rss(("Fed holds rates", "b", None))), now=at_cutoff)

    rows = sorted(await articles(newsroom_database), key=lambda row: row.first_seen_at)
    assert [row.edition_date for row in rows] == [date(2026, 9, 3), date(2026, 9, 4)]
    assert [row.edition_date for row in rows] == [
        clock.edition_date_for(before_cutoff.now),
        clock.edition_date_for(at_cutoff.now),
    ]


async def test_claim_only_takes_due_enabled_feed_sources_and_leases_them(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    due = await add_source(newsroom_database, key="due")
    await add_source(newsroom_database, key="later", next_poll_at=NOW + timedelta(minutes=5))
    await add_source(newsroom_database, key="off", enabled=False)
    await add_source(newsroom_database, key="manual", kind="manual", url=None)

    async with newsroom_database() as database:
        assert await claim_due_sources(database, NOW) == [due]
    async with newsroom_database() as database:
        assert await claim_due_sources(database, NOW) == []


async def test_failures_accumulate_and_notify_once_per_outage(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await add_source(newsroom_database, last_success_at=NOW - timedelta(hours=7))
    notifier = LogNotifier()
    broken = serving(lambda _: httpx.Response(503))
    now = Clock(NOW)

    async def tick(handler: Handler) -> None:
        async with newsroom_database() as database:
            await database.execute(
                NewsroomSource.__table__.update().values(next_poll_at=None)  # type: ignore[attr-defined]
            )
            await database.commit()
        await poll(newsroom_database, handler, now=now, notifier=notifier)
        now.now += timedelta(minutes=30)

    await tick(broken)
    await tick(broken)
    source = await source_row(newsroom_database, source_id)
    assert source.consecutive_failures == 2
    assert source.last_error_code == "http_503"
    assert source.unhealthy_notified_at is not None
    assert [notice.kind for notice in notifier.sent] == ["source_unhealthy"]
    assert "Example News" in notifier.sent[0].title

    await tick(serving(rss(("Recovered", "r", None))))
    source = await source_row(newsroom_database, source_id)
    assert (source.consecutive_failures, source.unhealthy_notified_at) == (0, None)
    assert len(notifier.sent) == 1


async def test_recent_success_does_not_notify(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    await add_source(newsroom_database, last_success_at=NOW - timedelta(hours=5))
    notifier = LogNotifier()

    [outcome] = await poll(newsroom_database, lambda _: httpx.Response(500), notifier=notifier)

    assert outcome.status == "failed"
    assert notifier.sent == []


async def test_missing_credentials_and_blocked_hosts_skip_without_failing(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    guardian = await add_source(
        newsroom_database,
        key="guardian",
        kind="guardian_api",
        url="https://content.guardianapis.com/search?section=business",
        full_text_in_feed=True,
    )
    sec = await add_source(
        newsroom_database, key="sec", kind="atom", options={"requires_contact_email": True}
    )
    blocked = await add_source(newsroom_database, key="blocked", hostname="blocked.example.com")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(500)

    outcomes = await poll(newsroom_database, handler, news_blocked_hostnames="blocked.example.com")

    assert {outcome.status for outcome in outcomes} == {"skipped"}
    assert requested == []
    for source_id, code in (
        (guardian, "guardian_api_key_missing"),
        (sec, "sec_contact_email_missing"),
        (blocked, "host_blocked"),
    ):
        row = await source_row(newsroom_database, source_id)
        assert (row.consecutive_failures, row.last_error_code) == (0, code)
        assert row.next_poll_at == NOW + timedelta(minutes=30)


GUARDIAN_JSON = json.dumps(
    {
        "response": {
            "results": [
                {
                    "webUrl": f"https://{HOST}/business/fed-holds",
                    "webTitle": "Fed holds rates",
                    "webPublicationDate": "2026-09-02T02:30:00Z",
                    "fields": {
                        "bodyText": "The Fed holds rates steady as inflation cools. " * 6,
                        "trailText": "<p>Policy unchanged</p>",
                    },
                },
                {
                    "webUrl": f"https://{HOST}/business/teaser",
                    "webTitle": "Teaser only",
                    "webPublicationDate": "2026-09-02T01:00:00Z",
                    "fields": {"bodyText": "Too short."},
                },
            ]
        }
    }
).encode()


async def test_guardian_key_is_sent_and_full_text_kept(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    await add_source(
        newsroom_database,
        kind="guardian_api",
        url="https://content.guardianapis.com/search?section=business",
        link_pattern=None,
        full_text_in_feed=True,
    )
    keys: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.url.params.get("api-key"))
        return httpx.Response(200, content=GUARDIAN_JSON)

    [outcome] = await poll(
        newsroom_database, serving(handler), guardian_api_key=SecretStr("secret")
    )

    assert outcome.inserted == 2
    assert keys == ["secret"]
    rows = {row.title: row for row in await articles(newsroom_database)}
    assert rows["Fed holds rates"].body_status == "ok"
    assert rows["Fed holds rates"].feed_summary == "Policy unchanged"
    assert rows["Teaser only"].fetch_status == "pending"


# --- FETCH stage --------------------------------------------------------------


ARTICLE_HTML = (
    "<html><head><title>Fed holds rates steady</title></head><body><article>"
    + "The Federal Reserve holds rates steady as inflation cools in the economy. " * 6
    + "</article></body></html>"
)


async def add_article(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: uuid.UUID,
    *,
    title: str = "Fed holds rates steady",
    url: str | None = None,
    **overrides: Any,
) -> uuid.UUID:
    link = url or f"https://{HOST}/news/{uuid.uuid4().hex}"
    async with session_factory() as database:
        values: dict[str, Any] = {
            "first_seen_at": NOW,
            "edition_date": date(2026, 9, 3),
            "fetch_status": "pending",
            "embed_status": "pending",
            **overrides,
        }
        article = NewsroomArticle(
            source_id=source_id, url=link, url_hash=url_hash(link), title=title, **values
        )
        database.add(article)
        await database.commit()
        return article.id


async def run_fetch(
    session_factory: async_sessionmaker[AsyncSession], handler: Handler, **overrides: Any
) -> str:
    async with session_factory() as database:
        [claimed] = await claim(database, queue.FETCH)
    stage = make_fetch_handler(settings(**overrides), deps(handler))
    return await run_claimed(session_factory, claimed, stage)


async def article_row(
    session_factory: async_sessionmaker[AsyncSession], article_id: uuid.UUID
) -> NewsroomArticle:
    async with session_factory() as database:
        row = await database.get(NewsroomArticle, article_id)
        assert row is not None
        return row


def page(body: str = ARTICLE_HTML, status: int = 200) -> Handler:
    return serving(
        lambda _: httpx.Response(status, text=body, headers={"content-type": "text/html"})
    )


async def test_fetch_stores_a_good_body(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    article_id = await add_article(newsroom_database, await add_source(newsroom_database))

    assert await run_fetch(newsroom_database, page()) == "done"

    row = await article_row(newsroom_database, article_id)
    assert (row.fetch_status, row.body_status, row.body_source) == ("done", "ok", "fetch")
    assert row.body is not None and row.body.startswith("The Federal Reserve")
    assert row.body_fetched_at == NOW and row.body_quality_reason is None


async def test_fetch_rejects_chrome_and_marks_robots_unavailable_as_done(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await add_source(newsroom_database)
    rejected = await add_article(newsroom_database, source_id)
    await run_fetch(newsroom_database, page("<article>Subscribe to keep reading.</article>"))
    blocked = await add_article(newsroom_database, source_id)
    await run_fetch(newsroom_database, serving(page(), robots="User-agent: *\nDisallow: /\n"))

    row = await article_row(newsroom_database, rejected)
    assert (row.fetch_status, row.body_status, row.body_quality_reason) == (
        "done",
        "rejected",
        "too_short",
    )
    row = await article_row(newsroom_database, blocked)
    assert (row.fetch_status, row.body_status, row.body_quality_reason) == (
        "done",
        "unavailable",
        "robots_disallowed",
    )
    assert row.body is None


async def test_fetch_retries_transient_failures(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    article_id = await add_article(newsroom_database, await add_source(newsroom_database))

    assert await run_fetch(newsroom_database, page(status=503)) == "pending"

    row = await article_row(newsroom_database, article_id)
    assert (row.fetch_status, row.fetch_attempts, row.fetch_error_code) == (
        "pending",
        1,
        "fetch_http_503",
    )
    assert row.body_status == "pending"


async def test_fetch_of_a_host_no_longer_allowlisted_is_unavailable(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    article_id = await add_article(newsroom_database, await add_source(newsroom_database))

    await run_fetch(newsroom_database, page(), news_blocked_hostnames=HOST)

    row = await article_row(newsroom_database, article_id)
    assert (row.body_status, row.body_quality_reason) == ("unavailable", "host_not_allowed")


async def test_manual_fetch_takes_the_page_title_and_queues_embedding(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    manual = await add_source(
        newsroom_database, key="manual", kind="manual", url=None, hostname="manual.invalid"
    )
    url = "https://elsewhere.example.org/story"
    article_id = await add_article(
        newsroom_database, manual, title=url, url=url, embed_status="idle"
    )

    await run_fetch(newsroom_database, page())

    row = await article_row(newsroom_database, article_id)
    assert row.title == "Fed holds rates steady"
    assert (row.body_status, row.embed_status) == ("ok", "pending")


async def test_exhausted_manual_fetch_still_queues_embedding(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    manual = await add_source(
        newsroom_database, key="manual", kind="manual", url=None, hostname="manual.invalid"
    )
    url = "https://elsewhere.example.org/story"
    article_id = await add_article(
        newsroom_database, manual, title=url, url=url, embed_status="idle", fetch_status="failed"
    )

    await poll(newsroom_database, serving(rss()))

    assert (await article_row(newsroom_database, article_id)).embed_status == "pending"


# --- purge --------------------------------------------------------------------


async def test_purge_clears_old_bodies_once_and_keeps_metadata(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    source_id = await add_source(newsroom_database)
    now = datetime(2026, 10, 1, 19, 30, tzinfo=UTC)  # 03:30 Taipei
    old = await add_article(
        newsroom_database,
        source_id,
        title="Old",
        body="text",
        body_status="ok",
        first_seen_at=now - timedelta(days=31),
    )
    pending_old = await add_article(
        newsroom_database, source_id, title="Old pending", first_seen_at=now - timedelta(days=40)
    )
    recent = await add_article(
        newsroom_database,
        source_id,
        title="Recent",
        body="text",
        body_status="ok",
        first_seen_at=now - timedelta(days=29),
    )

    assert await purge_old_bodies(newsroom_database, now, batch=1) == 2
    assert await purge_old_bodies(newsroom_database, now + timedelta(minutes=10)) == 0

    row = await article_row(newsroom_database, old)
    assert (row.body, row.body_status, row.title) == (None, "purged", "Old")
    assert (await article_row(newsroom_database, pending_old)).body_status == "purged"
    assert (await article_row(newsroom_database, recent)).body == "text"


# --- sources_service ------------------------------------------------------------


async def edit_log(session_factory: async_sessionmaker[AsyncSession]) -> list[NewsroomEditLog]:
    async with session_factory() as database:
        return list(
            (
                await database.scalars(select(NewsroomEditLog).order_by(NewsroomEditLog.created_at))
            ).all()
        )


async def test_create_and_update_source_write_the_edit_log(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await add_user(newsroom_database)
    data = SourceInput(
        key="reuters-markets",
        name="Reuters",
        kind="rss",
        url="https://feeds.example.com/markets.xml",
        hostname="WWW.Reuters.com.",
        markets=("global", "us_equity"),
        trust_tier=2,
        language_filter=("en",),
    )
    async with newsroom_database() as database:
        source_id = await sources_service.create_source(database, data, user_id=user_id)
        await database.commit()
    async with newsroom_database() as database:
        with pytest.raises(SourceConflictError):
            await sources_service.create_source(database, data, user_id=user_id)
        with pytest.raises(SourceValidationError, match="trust_tier"):
            await sources_service.create_source(
                database,
                replace(data, key="other", trust_tier=4),
                user_id=user_id,
            )
        with pytest.raises(SourceValidationError, match="HTTPS"):
            await sources_service.create_source(
                database,
                replace(data, key="other", url="http://x.example.com/f"),
                user_id=user_id,
            )

    source = await source_row(newsroom_database, source_id)
    assert source.hostname == "www.reuters.com"
    assert source.markets == ["global", "us_equity"] and source.next_poll_at is None

    async with newsroom_database() as database:
        await sources_service.update_source(
            database, source_id, {"enabled": False, "weight": 1.5}, user_id=user_id
        )
        await database.commit()
    async with newsroom_database() as database:
        await database.execute(
            NewsroomSource.__table__.update().values(next_poll_at=NOW)  # type: ignore[attr-defined]
        )
        await database.commit()
    async with newsroom_database() as database:
        await sources_service.update_source(database, source_id, {"enabled": True}, user_id=user_id)
        with pytest.raises(SourceValidationError, match="unknown source fields"):
            await sources_service.update_source(
                database, source_id, {"options": {}}, user_id=user_id
            )
        await database.commit()
    async with newsroom_database() as database:
        with pytest.raises(LookupError):
            await sources_service.update_source(
                database, uuid.uuid4(), {"enabled": True}, user_id=user_id
            )

    source = await source_row(newsroom_database, source_id)
    assert (source.enabled, source.weight, source.next_poll_at) == (True, 1.5, None)
    log = await edit_log(newsroom_database)
    assert [entry.action for entry in log] == ["create_source", "update_source", "update_source"]
    assert all(entry.user_id == user_id and entry.entity_id == source_id for entry in log)
    assert log[1].before == {"enabled": True, "weight": 1.0}
    assert log[1].after == {"enabled": False, "weight": 1.5}
    assert log[2].after == {"enabled": True}


async def test_manual_source_only_accepts_presentation_fields(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await add_user(newsroom_database)
    manual = await add_source(
        newsroom_database, key="manual", kind="manual", url=None, hostname="manual.invalid"
    )
    async with newsroom_database() as database:
        with pytest.raises(SourceValidationError, match="manual source"):
            await sources_service.update_source(
                database, manual, {"url": "https://x.example.com/f"}, user_id=user_id
            )
        await sources_service.update_source(database, manual, {"trust_tier": 3}, user_id=user_id)
        await database.commit()
    assert (await source_row(newsroom_database, manual)).trust_tier == 3


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    answers: dict[str, list[str]] = {}

    async def resolver(host: str, port: int) -> list[str]:
        del port
        return answers.get(host, ["93.184.216.34"])

    monkeypatch.setattr(safe_http, "resolve_addresses", resolver)
    return answers


async def test_submit_manual_url_queues_fetch_and_dedupes(
    newsroom_database: async_sessionmaker[AsyncSession],
    public_dns: dict[str, list[str]],
) -> None:
    user_id = await add_user(newsroom_database)
    manual = await add_source(
        newsroom_database, key="manual", kind="manual", url=None, hostname="manual.invalid"
    )
    async with newsroom_database() as database:
        article_id = await sources_service.submit_manual_url(
            database,
            "https://Elsewhere.example.org/story?id=7#comments",
            edition_date=date(2026, 9, 3),
            user_id=user_id,
        )
        await database.commit()
    async with newsroom_database() as database:
        again = await sources_service.submit_manual_url(
            database,
            "https://elsewhere.example.org/story?id=7",
            edition_date=date(2026, 9, 4),
            user_id=user_id,
        )
        await database.commit()

    assert again == article_id
    row = await article_row(newsroom_database, article_id)
    assert row.source_id == manual and row.submitted_by_user_id == user_id
    assert row.url == "https://elsewhere.example.org/story?id=7"
    assert row.edition_date == date(2026, 9, 3)
    assert (row.fetch_status, row.embed_status, row.body_status) == ("pending", "idle", "pending")
    log = await edit_log(newsroom_database)
    assert [(entry.action, entry.entity_id) for entry in log] == [
        ("submit_manual_url", article_id),
        ("submit_manual_url", article_id),
    ]
    assert log[1].after == {"url": row.url, "duplicate": True}


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://elsewhere.example.org/a", "HTTPS"),
        ("https://user:pw@elsewhere.example.org/a", "credentials"),
        ("https://elsewhere.example.org:8443/a", "HTTPS"),
        ("https://127.0.0.1/a", "IP literal"),
        ("https://intranet.example.org/a", "public"),
        ("https://blocked.example.org/a", "blocked"),
    ],
)
async def test_submit_manual_url_enforces_ssrf_rules(
    newsroom_database: async_sessionmaker[AsyncSession],
    public_dns: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    message: str,
) -> None:
    public_dns["intranet.example.org"] = ["10.1.2.3"]
    monkeypatch.setenv("DAILY_INSIGHTS_NEWS_BLOCKED_HOSTNAMES", "blocked.example.org")
    get_settings.cache_clear()
    user_id = await add_user(newsroom_database)
    await add_source(
        newsroom_database, key="manual", kind="manual", url=None, hostname="manual.invalid"
    )
    try:
        async with newsroom_database() as database:
            with pytest.raises(SourceValidationError, match=message):
                await sources_service.submit_manual_url(
                    database, url, edition_date=date(2026, 9, 3), user_id=user_id
                )
    finally:
        get_settings.cache_clear()
    async with newsroom_database() as database:
        count = await database.scalar(select(func.count()).select_from(NewsroomArticle))
    assert count == 0


async def test_set_manual_body_settles_fetch_and_requeues_needs_body_analysis(
    newsroom_database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await add_user(newsroom_database)
    source_id = await add_source(newsroom_database)
    async with newsroom_database() as database:
        event = NewsroomEvent(
            edition_date=date(2026, 9, 3),
            working_title="Fed holds",
            analysis_status="needs_body",
            analysis_attempts=2,
        )
        database.add(event)
        await database.commit()
        event_id = event.id
    article_id = await add_article(
        newsroom_database,
        source_id,
        event_id=event_id,
        body_status="unavailable",
        body_quality_reason="access_denied",
    )

    async with newsroom_database() as database:
        await sources_service.set_manual_body(
            database, article_id, "  Pasted body text.  " + "x" * 50_000, user_id=user_id
        )
        await database.commit()
    async with newsroom_database() as database:
        with pytest.raises(SourceValidationError):
            await sources_service.set_manual_body(database, article_id, "   ", user_id=user_id)
        with pytest.raises(LookupError):
            await sources_service.set_manual_body(database, uuid.uuid4(), "x", user_id=user_id)

    row = await article_row(newsroom_database, article_id)
    assert row.body is not None and row.body.startswith("Pasted body text.")
    assert len(row.body) == 40_000
    assert (row.body_status, row.body_source, row.body_quality_reason) == ("ok", "manual", None)
    assert row.fetch_status == "done"
    async with newsroom_database() as database:
        refreshed = await database.get(NewsroomEvent, event_id)
        assert refreshed is not None
        assert (refreshed.analysis_status, refreshed.analysis_attempts) == ("pending", 0)
    [entry] = await edit_log(newsroom_database)
    assert (entry.action, entry.entity_type, entry.entity_id) == (
        "set_manual_body",
        "article",
        article_id,
    )
    assert entry.before is not None and entry.before["body_status"] == "unavailable"
    assert entry.after is not None and entry.after["body_chars"] == 40_000
