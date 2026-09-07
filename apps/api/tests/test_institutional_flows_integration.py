import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from conftest import remigrate_database, reset_database_schema
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_health import readiness

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.modules.data_sources.api import (
    TwseMarketFlow,
    TwseMarketFlows,
    TwseStockFlow,
    TwseStockFlows,
)
from daily_insights_api.modules.identity.api import AuthContext, require_password_changed
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.modules.markets.api import (
    store_institutional_market_flows,
    store_institutional_stock_flows,
    stored_flow_dates,
)
from daily_insights_api.modules.markets.models import (
    InstitutionalMarketFlow,
    InstitutionalStockFlow,
    OrganizationMarketPolicy,
)
from daily_insights_api.modules.tenancy.models import Organization
from daily_insights_api.web.app import create_app

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    await asyncio.to_thread(remigrate_database, database_url)
    engine = create_async_engine(database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            await engine.dispose()
        finally:
            await asyncio.to_thread(reset_database_schema, database_url)


def _market(trade_date: date, foreign_net: int, fetched_at: datetime) -> TwseMarketFlows:
    return TwseMarketFlows(
        trade_date=trade_date,
        items=(
            TwseMarketFlow("foreign", 100 + foreign_net, 100, foreign_net),
            TwseMarketFlow("trust", 10, 4, 6),
        ),
        fetched_at=fetched_at,
    )


@pytest.mark.asyncio
async def test_refetching_a_day_rewrites_rows_instead_of_duplicating(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = datetime(2026, 9, 4, 8, tzinfo=UTC)
    second = datetime(2026, 9, 5, 8, tzinfo=UTC)
    async with session_factory.begin() as database:
        assert (
            await store_institutional_market_flows(
                database, market_code="tw_equity", flows=_market(date(2026, 9, 4), 5, first)
            )
            == 2
        )
        await store_institutional_market_flows(
            database, market_code="tw_equity", flows=_market(date(2026, 9, 3), 1, first)
        )
    async with session_factory.begin() as database:
        await store_institutional_market_flows(
            database, market_code="tw_equity", flows=_market(date(2026, 9, 4), 7, second)
        )
        stock = TwseStockFlows(
            trade_date=date(2026, 9, 4),
            items=(TwseStockFlow("2324", "仁寶", "foreign", 30, 10, 20),),
            fetched_at=second,
        )
        assert (
            await store_institutional_stock_flows(database, market_code="tw_equity", flows=stock)
            == 1
        )
        await store_institutional_stock_flows(database, market_code="tw_equity", flows=stock)

    async with session_factory() as database:
        assert await database.scalar(select(func.count()).select_from(InstitutionalMarketFlow)) == 4
        foreign = await database.scalar(
            select(InstitutionalMarketFlow).where(
                InstitutionalMarketFlow.trade_date == date(2026, 9, 4),
                InstitutionalMarketFlow.investor_type == "foreign",
            )
        )
        assert foreign is not None
        assert foreign.net_amount == 7 and foreign.source_fetched_at == second
        assert await database.scalar(select(func.count()).select_from(InstitutionalStockFlow)) == 1
        assert await stored_flow_dates(
            database,
            flows=InstitutionalMarketFlow,
            market_code="tw_equity",
            on_or_before=date(2026, 9, 4),
            limit=1,
        ) == {date(2026, 9, 4)}
        assert await stored_flow_dates(
            database,
            flows=InstitutionalMarketFlow,
            market_code="tw_equity",
            on_or_before=date(2026, 9, 3),
            limit=40,
        ) == {date(2026, 9, 3)}
        assert await stored_flow_dates(
            database,
            flows=InstitutionalStockFlow,
            market_code="tw_equity",
            on_or_before=date(2026, 9, 4),
            limit=7,
        ) == {date(2026, 9, 4)}


def _market_day(trade_date: date, nets: dict[str, int], fetched_at: datetime) -> TwseMarketFlows:
    """One day of market flows; the table's CHECK wants net == buy - sell."""
    return TwseMarketFlows(
        trade_date=trade_date,
        items=tuple(
            TwseMarketFlow(investor_type, max(net, 0), max(-net, 0), net)
            for investor_type, net in nets.items()
        ),
        fetched_at=fetched_at,
    )


def _stock_day(
    trade_date: date, nets: dict[tuple[str, str], int], fetched_at: datetime
) -> TwseStockFlows:
    return TwseStockFlows(
        trade_date=trade_date,
        items=tuple(
            TwseStockFlow(symbol, f"公司{symbol}", investor_type, max(net, 0), max(-net, 0), net)
            for (symbol, investor_type), net in nets.items()
        ),
        fetched_at=fetched_at,
    )


# A member of some organization with no policy rows, so every market is visible.
DEFAULT_ORGANIZATION_ID = uuid.uuid4()


def _signed_in_client(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    role: SystemRole = SystemRole.ORG_MEMBER,
    organization_id: uuid.UUID | None = DEFAULT_ORGANIZATION_ID,
) -> AsyncClient:
    app = create_app(Settings(environment="test"), readiness(True), session_factory)

    async def auth() -> AuthContext:
        return AuthContext(
            user=User(system_role=role), session=Session(), organization_id=organization_id
        )

    app.dependency_overrides[require_password_changed] = auth
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_market_flow_series_folds_five_categories_and_reports_in_hundred_millions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fetched_at = datetime(2026, 9, 4, 8, tzinfo=UTC)
    # Dated relative to today so the default 100-day window keeps containing
    # them; fixed dates would age out and leave the assertions meaningless.
    today = datetime.now(UTC).date()
    recent, earlier, ancient = (
        today - timedelta(days=1),
        today - timedelta(days=2),
        today - timedelta(days=400),
    )
    async with session_factory.begin() as database:
        for day in (earlier, recent, ancient):
            await store_institutional_market_flows(
                database,
                market_code="tw_equity",
                flows=_market_day(
                    day,
                    {
                        # The real BFI82U row for 2026-09-04, so `total` lands on
                        # the 合計 TWSE published that day.
                        "foreign": 56_212_953_803,
                        "foreign_dealer": 0,
                        "trust": -910_866_463,
                        "dealer_self": 1_506_303_813,
                        "dealer_hedge": 4_863_757_431,
                    },
                    fetched_at,
                ),
            )

    async with _signed_in_client(session_factory) as client:
        response = await client.get("/api/markets/tw/institutional-flows")
        windowed = await client.get(
            "/api/markets/tw/institutional-flows",
            params={"start": recent.isoformat(), "end": recent.isoformat()},
        )
        inverted = await client.get(
            "/api/markets/tw/institutional-flows",
            params={"start": recent.isoformat(), "end": earlier.isoformat()},
        )
        too_wide = await client.get(
            "/api/markets/tw/institutional-flows",
            params={"start": ancient.isoformat(), "end": recent.isoformat()},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    # Oldest first, in 億元, with the day outside the default window left out.
    assert [point["trade_date"] for point in body["series"]] == [
        earlier.isoformat(),
        recent.isoformat(),
    ]
    assert body["as_of"] == recent.isoformat()
    assert len(body["contract_hash"]) == 64
    assert body["endpoint"] == "/rwd/zh/fund/BFI82U"
    assert body["series"][0] == {
        "trade_date": earlier.isoformat(),
        "foreign": "562.12953803",
        "trust": "-9.10866463",
        "dealer": "63.70061244",
        # TWSE's own 合計 row for that day, to the cent.
        "total": "616.72148584",
    }
    assert [point["trade_date"] for point in windowed.json()["series"]] == [recent.isoformat()]
    assert inverted.status_code == 422
    assert too_wide.status_code == 422
    assert too_wide.json()["detail"] == "date range must not exceed 180 days"


@pytest.mark.asyncio
async def test_stock_rows_report_lots_for_the_latest_stored_day(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fetched_at = datetime(2026, 9, 4, 8, tzinfo=UTC)
    async with session_factory.begin() as database:
        # 2324 仁寶's real 2026-09-04 numbers, so the fold is pinned to a row
        # whose 三大法人買賣超股數 TWSE publishes: 71,016,331 shares.
        await store_institutional_stock_flows(
            database,
            market_code="tw_equity",
            flows=_stock_day(
                date(2026, 9, 4),
                {
                    ("2324", "foreign"): 69_366_284,
                    ("2324", "foreign_dealer"): 0,
                    ("2324", "trust"): -21_000,
                    ("2324", "dealer_self"): 200_115,
                    ("2324", "dealer_hedge"): 1_470_932,
                    ("1101", "foreign"): -5_000,
                    ("1101", "foreign_dealer"): 0,
                    ("1101", "trust"): 0,
                    ("1101", "dealer_self"): 0,
                    ("1101", "dealer_hedge"): 0,
                },
                fetched_at,
            ),
        )
        # An older day the request must not reach for once a newer one exists.
        await store_institutional_stock_flows(
            database,
            market_code="tw_equity",
            flows=_stock_day(date(2026, 9, 3), {("9999", "foreign"): 1_000}, fetched_at),
        )

    async with _signed_in_client(session_factory) as client:
        latest = await client.get(
            "/api/markets/tw/institutional-stocks", params={"date": "2026-09-05"}
        )
        earlier = await client.get(
            "/api/markets/tw/institutional-stocks", params={"date": "2026-09-03", "locale": "en"}
        )
        before_any = await client.get(
            "/api/markets/tw/institutional-stocks", params={"date": "2026-01-01"}
        )

    assert latest.status_code == 200, latest.text
    body = latest.json()
    assert body["as_of"] == "2026-09-04"
    assert body["endpoint"] == "/rwd/zh/fund/T86"
    # Largest net buy first, and shares divided into lots.
    assert body["rows"] == [
        {
            "symbol": "2324",
            "name": "公司2324",
            "foreign_lots": "69366.284",
            "trust_lots": "-21",
            "dealer_lots": "1671.047",
            "total_lots": "71016.331",
        },
        {
            "symbol": "1101",
            "name": "公司1101",
            "foreign_lots": "-5",
            "trust_lots": "0",
            "dealer_lots": "0",
            "total_lots": "-5",
        },
    ]
    # A date before the newest one reads that older day instead, and the locale
    # is accepted even though TWSE publishes no English security names.
    assert [row["symbol"] for row in earlier.json()["rows"]] == ["9999"]
    assert before_any.json() == {
        "as_of": None,
        "contract_version": body["contract_version"],
        "contract_hash": body["contract_hash"],
        "endpoint": "/rwd/zh/fund/T86",
        "rows": [],
    }


@pytest.mark.asyncio
async def test_institutional_endpoints_are_hidden_when_the_contract_excludes_taiwan(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    hidden_from = uuid.uuid4()
    author_id = uuid.uuid4()
    async with session_factory.begin() as database:
        database.add_all(
            [
                Organization(id=hidden_from, name="Contract A", slug="contract-a", seat_limit=1),
                User(
                    id=author_id,
                    email="policy-author@example.com",
                    display_name="Policy Author",
                    # This test never authenticates; the column is only NOT NULL.
                    password_hash="unused",
                    must_change_password=False,
                    system_role=SystemRole.ADMIN,
                    status=UserStatus.ACTIVE,
                ),
            ]
        )
        await database.flush()
        database.add(
            OrganizationMarketPolicy(
                organization_id=hidden_from,
                market_code="tw_equity",
                is_visible=False,
                changed_by_user_id=author_id,
                changed_at=datetime.now(UTC),
            )
        )

    async with _signed_in_client(session_factory, organization_id=hidden_from) as blocked:
        flows = await blocked.get("/api/markets/tw/institutional-flows")
        stocks = await blocked.get("/api/markets/tw/institutional-stocks")
    # Internal staff belong to no organization and still get their own preview.
    async with _signed_in_client(
        session_factory, role=SystemRole.ADMIN, organization_id=None
    ) as staff:
        allowed = await staff.get("/api/markets/tw/institutional-flows")

    assert flows.status_code == 404 and stocks.status_code == 404
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["series"] == []
