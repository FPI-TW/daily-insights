import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
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
            source_hostname="www.reuters.com",
            source_url="https://www.reuters.com/article",
            source_published_at=None,
            numeric_facts=["+3.2%", "1 碼"],
            market="us",
            event_key="fed-rate-decision",
        )

    async def scalars(self, statement: object) -> _Scalars:
        del statement
        return _Scalars(self.edition)

    async def execute(self, statement: object) -> _Rows:
        params = statement.compile().params  # type: ignore[attr-defined]
        locale = next(value for value in params.values() if value in {"zh-hant", "zh-hans", "en"})
        return _Rows(
            [
                (
                    self.item,
                    SimpleNamespace(
                        headline=f"{locale} headline",
                        summary=f"{locale} summary",
                    ),
                )
            ]
        )


async def test_latest_news_requires_authentication() -> None:
    app = create_app(Settings(environment="test"), readiness_checker=lambda: _ready())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/news/latest")
    assert response.status_code == 401


async def test_latest_news_is_no_store_unavailable_without_history_and_validates_locale() -> None:
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
    assert all(response.json()["caveat"] is None for response in responses.values())
    assert all(response.json()["items"] for response in responses.values())
    assert {
        locale: response.json()["items"][0]["headline"] for locale, response in responses.items()
    } == {
        "zh-hant": "zh-hant headline",
        "zh-hans": "zh-hans headline",
        "en": "en headline",
    }


def test_localized_caveat_only_marks_unavailable_editions() -> None:
    assert _localized_caveat("complete", 5, "en") is None
    assert _localized_caveat("complete", 4, "en") is None
    assert _localized_caveat("partial", 1, "zh-hant") is None
    assert _localized_caveat("unavailable", 0, "en") == (
        "Today's major news has not been generated."
    )


async def _ready() -> bool:
    return True


def _member(role: str, organization_id: uuid.UUID | None) -> AuthContext:
    return cast(
        AuthContext,
        SimpleNamespace(
            user=SimpleNamespace(system_role=role),
            organization_id=organization_id,
        ),
    )


async def test_market_news_is_policy_gated_and_unknown_markets_are_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.core.enums import SystemRole
    from daily_insights_api.modules.news import access as news_access

    app = create_app(Settings(environment="test"), readiness_checker=lambda: _ready())
    organization = uuid.uuid4()
    current = {"context": _member(SystemRole.ORG_MEMBER, organization)}

    async def auth() -> AuthContext:
        return current["context"]

    async def database():  # type: ignore[no-untyped-def]
        yield _Database()

    async def visible(database: object, organization_id: uuid.UUID) -> set[str]:
        del database
        assert organization_id == organization
        return {"us_equity", "crypto"}

    monkeypatch.setattr(news_access, "visible_market_codes", visible)
    app.dependency_overrides[require_password_changed] = auth
    app.dependency_overrides[get_database_session] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        visible_response = await client.get("/api/news/us_equity/latest?locale=en")
        hidden_response = await client.get("/api/news/tw_equity/latest?locale=en")
        unknown_response = await client.get("/api/news/crypto/latest?locale=en")
        current["context"] = _member(SystemRole.ADMIN, None)
        internal_response = await client.get("/api/news/tw_equity/latest?locale=zh-hant")
        current["context"] = _member(SystemRole.ORG_MEMBER, None)
        orphan_response = await client.get("/api/news/us_equity/latest")

    assert visible_response.status_code == 200
    assert visible_response.json()["market_code"] == "us_equity"
    assert visible_response.json()["target_items"] == 5
    assert visible_response.json()["status"] == "unavailable"
    assert hidden_response.status_code == 404
    assert unknown_response.status_code == 404
    assert internal_response.status_code == 200
    assert internal_response.json()["market_code"] == "tw_equity"
    assert orphan_response.status_code == 403
