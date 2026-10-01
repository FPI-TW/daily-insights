import uuid
from datetime import date
from types import SimpleNamespace
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.newsroom import public_api
from daily_insights_api.modules.newsroom.contracts import MarketCode
from daily_insights_api.web.app import create_app
from daily_insights_api.web.dependencies import get_database_session


async def _ready() -> bool:
    return True


def _member(role: str, organization_id: uuid.UUID | None) -> AuthContext:
    return cast(
        AuthContext,
        SimpleNamespace(user=SimpleNamespace(system_role=role), organization_id=organization_id),
    )


async def test_latest_edition_requires_authentication() -> None:
    app = create_app(Settings(environment="test"), readiness_checker=_ready)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/newsroom/editions/latest?market=global")
    assert response.status_code == 401


async def test_latest_edition_is_policy_gated_per_market(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(Settings(environment="test"), readiness_checker=_ready)
    organization = uuid.uuid4()
    current = {"context": _member(SystemRole.ORG_MEMBER, organization)}
    calls: list[tuple[str, str, frozenset[str]]] = []

    async def auth() -> AuthContext:
        return current["context"]

    async def database():  # type: ignore[no-untyped-def]
        yield object()

    async def visible(database: object, organization_id: uuid.UUID) -> set[str]:
        del database
        assert organization_id == organization
        return {"us_equity", "crypto"}

    async def latest(
        database: AsyncSession,
        market_code: MarketCode,
        locale: public_api.Locale,
        *,
        today: date,
        linkable_markets: frozenset[str] = frozenset(),
    ) -> public_api.NewsroomEditionResponse:
        del database, today
        calls.append((market_code, locale, linkable_markets))
        return public_api.NewsroomEditionResponse(
            market_code=market_code,
            locale=locale,
            edition_id=None,
            edition_date=None,
            is_today=False,
            published_at=None,
            items=[],
        )

    monkeypatch.setattr(public_api, "visible_market_codes", visible)
    monkeypatch.setattr(public_api, "latest_edition", latest)
    app.dependency_overrides[require_password_changed] = auth
    app.dependency_overrides[get_database_session] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

        async def status(path: str) -> int:
            return (await client.get(f"/api/newsroom/editions/latest?{path}")).status_code

        assert await status("market=us_equity&locale=en") == 200
        assert await status("market=tw_equity") == 404
        assert await status("market=global&locale=zh-hans") == 200
        assert await status("market=crypto") == 422
        assert await status("market=global&locale=fr") == 422
        assert await status("locale=en") == 422
        current["context"] = _member(SystemRole.ORG_MEMBER, None)
        assert await status("market=us_equity") == 403
        assert await status("market=global") == 200
        current["context"] = _member(SystemRole.ADMIN, None)
        assert await status("market=tw_equity") == 200

    member_links = frozenset({"us_equity", "crypto"})
    assert calls[0] == ("us_equity", "en", member_links)
    assert calls[1] == ("global", "zh-hans", member_links)
    # A member without an organization reads the global edition unlinked.
    assert calls[2] == ("global", "zh-hant", frozenset())
    assert calls[3][0] == "tw_equity"
    assert {"tw_equity", "us_equity", "hk_equity", "cn_equity"} <= calls[3][2]
