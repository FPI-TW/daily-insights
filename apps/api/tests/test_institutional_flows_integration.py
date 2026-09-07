import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

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
            user=User(system_role=role),
            session=Session(),
            organization_id=organization_id,
        )

    app.dependency_overrides[require_password_changed] = auth
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_market_flows_endpoint_folds_five_categories_into_three(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fetched_at = datetime(2026, 9, 4, 8, tzinfo=UTC)
    async with session_factory.begin() as database:
        for day, offset in ((date(2026, 9, 3), 0), (date(2026, 9, 4), 1)):
            await store_institutional_market_flows(
                database,
                market_code="tw_equity",
                flows=_market_day(
                    day,
                    {
                        "foreign": 100 + offset,
                        "foreign_dealer": 20,
                        "trust": -30,
                        "dealer_self": 7,
                        "dealer_hedge": -3,
                    },
                    fetched_at,
                ),
            )

    async with _signed_in_client(session_factory) as client:
        response = await client.get("/api/markets/institutional/market-flows")

    assert response.status_code == 200, response.text
    # Newest first, and the two dealer books and the two foreign books are summed.
    assert response.json() == [
        {"trade_date": "2026-09-04", "foreign": 121, "trust": -30, "dealer": 4},
        {"trade_date": "2026-09-03", "foreign": 120, "trust": -30, "dealer": 4},
    ]


@pytest.mark.asyncio
async def test_stock_flow_leaders_rank_within_each_investor_type(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fetched_at = datetime(2026, 9, 4, 8, tzinfo=UTC)
    day = date(2026, 9, 4)
    # Seven securities per investor type, so the fives have something to exclude.
    # Trust's numbers are far smaller than foreign's: one pooled ranking would
    # drop every trust row, which is the reason for ranking per investor type.
    nets = {(f"{2300 + index}", "foreign"): (index - 3) * 1_000_000 for index in range(7)}
    nets.update({(f"{2300 + index}", "trust"): (index - 3) * 10 for index in range(7)})
    async with session_factory.begin() as database:
        await store_institutional_stock_flows(
            database, market_code="tw_equity", flows=_stock_day(day, nets, fetched_at)
        )
        # A second day the request must not mix in.
        await store_institutional_stock_flows(
            database,
            market_code="tw_equity",
            flows=_stock_day(date(2026, 9, 3), {("9999", "foreign"): 99_000_000}, fetched_at),
        )

    async with _signed_in_client(session_factory) as client:
        latest = await client.get("/api/markets/institutional/stock-flows")
        earlier = await client.get(
            "/api/markets/institutional/stock-flows", params={"trade_date": "2026-09-03"}
        )

    assert latest.status_code == 200, latest.text
    body = latest.json()
    assert body["trade_date"] == "2026-09-04"
    by_type: dict[str, list[dict[str, object]]] = {"foreign": [], "trust": []}
    for row in body["top_buys"]:
        by_type[str(row["investor_type"])].append(row)
    assert [row["symbol"] for row in by_type["foreign"]] == ["2306", "2305", "2304", "2303", "2302"]
    assert [row["symbol"] for row in by_type["trust"]] == ["2306", "2305", "2304", "2303", "2302"]
    assert by_type["foreign"][0]["net_shares"] == 3_000_000
    assert by_type["trust"][0] == {
        "trade_date": "2026-09-04",
        "symbol": "2306",
        "security_name": "公司2306",
        "investor_type": "trust",
        "net_shares": 30,
    }
    sells = [row for row in body["top_sells"] if row["investor_type"] == "foreign"]
    assert [row["symbol"] for row in sells] == ["2300", "2301", "2302", "2303", "2304"]
    assert sells[0]["net_shares"] == -3_000_000
    # Both directions cover all five per type, for both types.
    assert len(body["top_buys"]) == len(body["top_sells"]) == 10

    assert earlier.status_code == 200, earlier.text
    assert [row["symbol"] for row in earlier.json()["top_buys"]] == ["9999"]


@pytest.mark.asyncio
async def test_institutional_endpoints_report_an_empty_store_without_failing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _signed_in_client(session_factory) as client:
        market = await client.get("/api/markets/institutional/market-flows")
        stock = await client.get("/api/markets/institutional/stock-flows")

    assert market.status_code == 200 and market.json() == []
    assert stock.status_code == 200
    assert stock.json() == {"trade_date": None, "top_buys": [], "top_sells": []}


@pytest.mark.asyncio
async def test_institutional_flows_are_hidden_when_the_contract_excludes_the_market(
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
        await store_institutional_market_flows(
            database,
            market_code="tw_equity",
            flows=_market_day(
                date(2026, 9, 4), {"foreign": 1}, datetime(2026, 9, 4, 8, tzinfo=UTC)
            ),
        )

    async with _signed_in_client(session_factory, organization_id=hidden_from) as blocked:
        market = await blocked.get("/api/markets/institutional/market-flows")
        stock = await blocked.get("/api/markets/institutional/stock-flows")
    # Internal staff belong to no organization and still get their own preview.
    async with _signed_in_client(
        session_factory, role=SystemRole.ADMIN, organization_id=None
    ) as staff:
        allowed = await staff.get("/api/markets/institutional/market-flows")

    assert market.status_code == 404 and stock.status_code == 404
    assert market.json()["detail"] == "market not found"
    assert allowed.status_code == 200, allowed.text
    assert [row["foreign"] for row in allowed.json()] == [1]
