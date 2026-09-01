import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from httpx import ASGITransport, AsyncClient

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.news.router import _localized_caveat
from daily_insights_api.web.app import create_app
from daily_insights_api.web.dependencies import get_database_session


class _Scalars:
    def __init__(self, value: object | None = None) -> None:
        self.value = value

    def first(self) -> object | None:
        return self.value


class _Database:
    async def scalars(self, statement: object) -> _Scalars:
        del statement
        return _Scalars()


class _Rows:
    def __init__(self, rows: list[tuple[object, object]]) -> None:
        self._rows = rows

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _EditionDatabase:
    def __init__(self) -> None:
        self.edition = SimpleNamespace(
            id=uuid.uuid4(),
            edition_date=datetime.now().date(),
            revision=2,
            generated_at=datetime.now(UTC),
            status="partial",
            caveat="1/5 stories completed",
        )
        self.item = SimpleNamespace(
            id=uuid.uuid4(),
            rank=1,
            importance=4,
            topic="markets",
            source_name="Reuters",
            source_url="https://www.reuters.com/article",
            source_published_at=None,
        )

    async def scalars(self, statement: object) -> _Scalars:
        del statement
        return _Scalars(self.edition)

    async def execute(self, statement: object) -> _Rows:
        del statement
        return _Rows([(self.item, SimpleNamespace(headline="Headline", summary="Summary"))])


async def test_latest_news_requires_authentication() -> None:
    app = create_app(Settings(environment="test"), readiness_checker=lambda: _ready())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/news/latest")
    assert response.status_code == 401


async def test_latest_news_is_no_store_today_only_unavailable_and_validates_locale() -> None:
    app = create_app(Settings(environment="test"), readiness_checker=lambda: _ready())

    async def auth() -> AuthContext:
        return cast(AuthContext, None)

    async def database():  # type: ignore[no-untyped-def]
        yield _Database()

    app.dependency_overrides[require_password_changed] = auth
    app.dependency_overrides[get_database_session] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = {
            locale: await client.get(f"/api/news/latest?locale={locale}")
            for locale in ("zh-hant", "zh-hans", "en")
        }
        invalid = await client.get("/api/news/latest?locale=fr")
    assert all(response.status_code == 200 for response in responses.values())
    assert all(response.headers["cache-control"] == "no-store" for response in responses.values())
    assert {locale: response.json()["caveat"] for locale, response in responses.items()} == {
        "zh-hant": "本日重大新聞尚未產生。",
        "zh-hans": "本日重大新闻尚未生成。",
        "en": "Today's major news has not been generated.",
    }
    assert all(response.json()["items"] == [] for response in responses.values())
    assert invalid.status_code == 422


async def test_latest_news_localizes_existing_partial_edition_caveat_for_every_locale() -> None:
    app = create_app(Settings(environment="test"), readiness_checker=lambda: _ready())

    async def auth() -> AuthContext:
        return cast(AuthContext, None)

    database = _EditionDatabase()

    async def dependency():  # type: ignore[no-untyped-def]
        yield database

    app.dependency_overrides[require_password_changed] = auth
    app.dependency_overrides[get_database_session] = dependency
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = {
            locale: await client.get(f"/api/news/latest?locale={locale}")
            for locale in ("zh-hant", "zh-hans", "en")
        }
    assert {locale: response.json()["caveat"] for locale, response in responses.items()} == {
        "zh-hant": "本日完成 1/5 則新聞，其餘資料暫缺。",  # noqa: RUF001
        "zh-hans": "本日完成 1/5 则新闻，其余资料暂缺。",  # noqa: RUF001
        "en": (
            "Today's edition contains 1/5 stories; "
            "the remaining coverage is temporarily unavailable."
        ),
    }
    assert all(response.json()["items"] for response in responses.values())


def test_localized_caveat_is_null_only_for_complete_five_item_editions() -> None:
    assert _localized_caveat("complete", 5, "en") is None
    assert (
        _localized_caveat("complete", 4, "en") == "Today's edition contains 4/5 stories; "
        "the remaining coverage is temporarily unavailable."
    )


async def _ready() -> bool:
    return True
