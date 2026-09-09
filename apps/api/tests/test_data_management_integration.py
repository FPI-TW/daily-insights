import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from conftest import remigrate_database
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_health import readiness

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.security import hash_password
from daily_insights_api.modules.audit.models import AuditEvent
from daily_insights_api.modules.data_management import service as data_management_service
from daily_insights_api.modules.data_management.models import DataManagementRun
from daily_insights_api.modules.data_management.service import (
    MACRO_EXECUTION_LOCK_KEY,
    RunAlreadyActiveError,
    cancel_run,
    claim_next_run,
    complete_news_run,
    complete_run,
    enqueue_automatic_macro_run,
    enqueue_automatic_news_all_run,
    enqueue_run,
    execution_lock_key,
    heartbeat_run,
    worker_loop,
)
from daily_insights_api.modules.identity.api import (
    AuthContext,
    require_csrf,
    require_password_changed,
)
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.web.app import create_app

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def data_management_database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.environ.get("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    remigrate_database(database_url)
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as database:
        database.add(
            User(
                email="data-management@example.com",
                display_name="Data Manager",
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
        user = await database.scalar(
            select(User).where(User.email == "data-management@example.com")
        )
    assert user is not None
    return user


def _admin_client(
    factory: async_sessionmaker[AsyncSession], user: User, *, enabled: bool
) -> AsyncClient:
    app = create_app(
        Settings(
            environment="test",
            morning_reports_enabled=enabled,
            twelve_data_api_key="test-key",
            yfinance_enabled=enabled,
        ),
        readiness(True),
        session_factory=factory,
    )

    async def authenticated() -> AuthContext:
        return AuthContext(user=user, session=Session(), organization_id=None)

    # These route-level tests intentionally authenticate at the dependency
    # boundary; authentication and CSRF validation themselves are covered by
    # identity tests. Keeping the database session real proves enqueue/list/get
    # and audit writes are wired through the deployed router.
    app.dependency_overrides[require_password_changed] = authenticated
    app.dependency_overrides[require_csrf] = authenticated
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_enqueue_partial_indexes_allow_index_but_reject_second_morning(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(data_management_database)
    async with data_management_database() as database:
        await enqueue_run(
            database,
            operation="morning_all",
            market_code=None,
            requester_id=user.id,
            request_id="a",
        )
    async with data_management_database() as database:
        with pytest.raises(RunAlreadyActiveError):
            await enqueue_run(
                database,
                operation="morning_market",
                market_code="crypto",
                requester_id=user.id,
                request_id="b",
            )
    async with data_management_database() as database:
        index = await enqueue_run(
            database,
            operation="index_yahoo",
            market_code=None,
            requester_id=user.id,
            request_id="c",
        )
    assert index.operation == "index_yahoo"


async def test_a_scheduled_run_is_stored_without_a_requester_and_still_locks_its_class(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(data_management_database)
    async with data_management_database() as database:
        scheduled = await enqueue_run(
            database,
            operation="institutional_twse",
            market_code=None,
            requester_id=None,
            request_id=None,
        )
    assert scheduled.requested_by_user_id is None
    # The lock is on the operation class, not on who asked, so an administrator
    # pressing the button while the scheduled run is queued gets refused rather
    # than a second walk against a source that allows one request per six
    # seconds.
    async with data_management_database() as database:
        with pytest.raises(RunAlreadyActiveError):
            await enqueue_run(
                database,
                operation="institutional_twse",
                market_code=None,
                requester_id=user.id,
                request_id="d",
            )


async def test_automatic_macro_persists_behind_manual_run_and_executes_once(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(data_management_database)
    edition = date(2026, 9, 8)
    async with data_management_database() as database:
        manual = await enqueue_run(
            database,
            operation="macro_dashboard",
            market_code=None,
            requester_id=user.id,
            request_id="manual",
            edition_date=edition,
        )

    assert (
        await enqueue_automatic_macro_run(data_management_database, edition_date=edition)
        == "queued"
    )
    # The scheduler's obligation is stored immediately and cannot disappear at
    # midnight. Claiming keeps the manual run ahead of its scheduled companion.
    claimed_manual = await claim_next_run(data_management_database, "manual-worker")
    assert claimed_manual is not None and claimed_manual.id == manual.id
    await complete_run(
        data_management_database,
        claimed_manual,
        "manual-worker",
        status="succeeded",
        result={},
    )
    claimed_automatic = await claim_next_run(data_management_database, "automatic-worker")
    assert claimed_automatic is not None
    assert claimed_automatic.operation == "macro_dashboard"
    assert claimed_automatic.requested_by_user_id is None
    await complete_run(
        data_management_database,
        claimed_automatic,
        "automatic-worker",
        status="succeeded",
        result={},
    )
    assert (
        await enqueue_automatic_macro_run(data_management_database, edition_date=edition)
        == "already_recorded"
    )
    async with data_management_database() as database:
        automatic = (
            await database.scalars(
                select(DataManagementRun).where(
                    DataManagementRun.operation == "macro_dashboard",
                    DataManagementRun.requested_by_user_id.is_(None),
                    DataManagementRun.edition_date == edition,
                )
            )
        ).all()
    assert len(automatic) == 1


async def test_automatic_news_retries_only_unsuccessful_markets_when_due(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    edition = date(2026, 9, 8)
    initial_at = datetime(2026, 9, 8, 8, tzinfo=ZoneInfo("Asia/Taipei"))
    concurrent_results = await asyncio.gather(
        enqueue_automatic_news_all_run(data_management_database, edition_date=edition),
        enqueue_automatic_news_all_run(data_management_database, edition_date=edition),
    )
    assert sorted(concurrent_results) == ["already_recorded", "queued"]
    assert (
        await enqueue_automatic_news_all_run(data_management_database, edition_date=edition)
        == "already_recorded"
    )
    claimed = await claim_next_run(data_management_database, "news-worker", now=initial_at)
    assert claimed is not None and claimed.operation == "news_all"
    await complete_news_run(
        data_management_database,
        claimed,
        "news-worker",
        status="partial",
        result={
            "outcome": "partial",
            "outcomes": {
                "global": "complete",
                "tw_equity": "partial",
                "us_equity": "unavailable",
            },
        },
        error="news_partial",
        now=initial_at,
    )
    async with data_management_database() as database:
        retries = (
            await database.scalars(
                select(DataManagementRun)
                .where(
                    DataManagementRun.operation == "news_market",
                    DataManagementRun.requested_by_user_id.is_(None),
                )
                .order_by(DataManagementRun.market_code)
            )
        ).all()
    assert [(retry.market_code, retry.scheduled_for) for retry in retries] == [
        ("tw_equity", datetime(2026, 9, 8, 8, 30, tzinfo=ZoneInfo("Asia/Taipei"))),
        ("us_equity", datetime(2026, 9, 8, 8, 30, tzinfo=ZoneInfo("Asia/Taipei"))),
    ]
    assert (
        await claim_next_run(
            data_management_database,
            "early-worker",
            now=datetime(2026, 9, 8, 8, 29, tzinfo=ZoneInfo("Asia/Taipei")),
        )
        is None
    )
    retry = await claim_next_run(
        data_management_database,
        "retry-worker",
        now=datetime(2026, 9, 8, 8, 30, tzinfo=ZoneInfo("Asia/Taipei")),
    )
    assert retry is not None and retry.market_code == "tw_equity"
    await complete_news_run(
        data_management_database,
        retry,
        "retry-worker",
        status="partial",
        result={"outcome": "partial", "outcomes": {"tw_equity": "partial"}},
        error="news_partial",
        now=datetime(2026, 9, 8, 11, 31, tzinfo=ZoneInfo("Asia/Taipei")),
    )
    async with data_management_database() as database:
        next_tw_retry = await database.scalar(
            select(DataManagementRun.id).where(
                DataManagementRun.operation == "news_market",
                DataManagementRun.market_code == "tw_equity",
                DataManagementRun.status == "pending",
            )
        )
        automatic_all = (
            await database.scalars(
                select(DataManagementRun).where(
                    DataManagementRun.operation == "news_all",
                    DataManagementRun.requested_by_user_id.is_(None),
                    DataManagementRun.edition_date == edition,
                )
            )
        ).all()
    assert next_tw_retry is None
    assert len(automatic_all) == 1


async def test_automatic_news_retry_is_cancelled_instead_of_claimed_after_deadline(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    edition = date(2026, 9, 8)
    scheduled_for = datetime(2026, 9, 8, 12, tzinfo=ZoneInfo("Asia/Taipei"))
    async with data_management_database.begin() as database:
        retry = DataManagementRun(
            operation="news_market",
            market_code="global",
            edition_date=edition,
            status="pending",
            requested_by_user_id=None,
            scheduled_for=scheduled_for,
        )
        database.add(retry)

    after_deadline = datetime(2026, 9, 8, 12, 0, 1, tzinfo=ZoneInfo("Asia/Taipei"))
    claimed = await claim_next_run(
        data_management_database,
        "late-worker",
        now=after_deadline,
    )
    assert claimed is None
    async with data_management_database() as database:
        expired = await database.get(DataManagementRun, retry.id)
    assert expired is not None
    assert expired.status == "cancelled"
    assert expired.completed_at == after_deadline
    assert expired.error == "news_window_expired"
    assert expired.result == {
        "outcome": "expired",
        "reason": "news_window_expired",
        "expired_at": after_deadline.isoformat(),
        "scheduled_for": scheduled_for.astimezone(UTC).isoformat(),
    }


async def test_automatic_news_retry_is_claimable_at_exact_deadline(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    deadline = datetime(2026, 9, 8, 12, tzinfo=ZoneInfo("Asia/Taipei"))
    async with data_management_database.begin() as database:
        retry = DataManagementRun(
            operation="news_market",
            market_code="global",
            edition_date=deadline.date(),
            status="pending",
            requested_by_user_id=None,
            scheduled_for=deadline,
        )
        database.add(retry)

    claimed = await claim_next_run(
        data_management_database,
        "deadline-worker",
        now=deadline,
    )
    assert claimed is not None
    assert claimed.id == retry.id
    assert claimed.status == "running"
    assert claimed.error is None


async def test_automatic_news_all_is_cancelled_instead_of_claimed_after_deadline(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    edition = date(2026, 9, 8)
    assert (
        await enqueue_automatic_news_all_run(data_management_database, edition_date=edition)
        == "queued"
    )

    after_deadline = datetime(2026, 9, 8, 12, 0, 1, tzinfo=ZoneInfo("Asia/Taipei"))
    claimed = await claim_next_run(data_management_database, "late-worker", now=after_deadline)

    assert claimed is None
    async with data_management_database() as database:
        expired = await database.scalar(
            select(DataManagementRun).where(
                DataManagementRun.operation == "news_all",
                DataManagementRun.edition_date == edition,
                DataManagementRun.requested_by_user_id.is_(None),
            )
        )
    assert expired is not None
    assert expired.status == "cancelled"
    assert expired.completed_at == after_deadline
    assert expired.error == "news_window_expired"
    assert expired.result == {
        "outcome": "expired",
        "reason": "news_window_expired",
        "expired_at": after_deadline.isoformat(),
        "scheduled_for": datetime(2026, 9, 8, 8, tzinfo=ZoneInfo("Asia/Taipei"))
        .astimezone(UTC)
        .isoformat(),
    }


async def test_automatic_news_all_is_claimable_at_exact_deadline(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    edition = date(2026, 9, 8)
    assert (
        await enqueue_automatic_news_all_run(data_management_database, edition_date=edition)
        == "queued"
    )

    deadline = datetime(2026, 9, 8, 12, tzinfo=ZoneInfo("Asia/Taipei"))
    claimed = await claim_next_run(data_management_database, "deadline-worker", now=deadline)

    assert claimed is not None
    assert claimed.operation == "news_all"
    assert claimed.edition_date == edition
    assert claimed.status == "running"
    assert claimed.error is None


async def test_due_automatic_news_retry_is_claimed_ahead_of_pending_manual_work(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(data_management_database)
    due_at = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Taipei"))
    async with data_management_database() as database:
        manual = await enqueue_run(
            database,
            operation="index_yahoo",
            market_code=None,
            requester_id=user.id,
            request_id="manual-ahead-of-news",
        )
    async with data_management_database.begin() as database:
        retry = DataManagementRun(
            operation="news_market",
            market_code="global",
            edition_date=due_at.date(),
            status="pending",
            requested_by_user_id=None,
            scheduled_for=due_at,
        )
        database.add(retry)

    claimed = await claim_next_run(data_management_database, "news-worker", now=due_at)

    assert claimed is not None
    assert claimed.id == retry.id
    async with data_management_database() as database:
        pending_manual = await database.get(DataManagementRun, manual.id)
    assert pending_manual is not None
    assert pending_manual.status == "pending"


async def test_cancelled_macro_cannot_overlap_another_macro_execution(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(data_management_database)
    async with data_management_database() as database:
        first = await enqueue_run(
            database,
            operation="macro_dashboard",
            market_code=None,
            requester_id=user.id,
            request_id="first",
        )
    claimed_first = await claim_next_run(data_management_database, "worker-one")
    assert claimed_first is not None and claimed_first.id == first.id

    # Model the first worker's execution session: cancellation changes its
    # durable row before its task observes the next heartbeat, but this group
    # lock remains held until the provider task exits.
    async with data_management_database() as execution_session:
        await execution_session.execute(select(func.pg_advisory_lock(MACRO_EXECUTION_LOCK_KEY)))
        try:
            async with data_management_database() as database:
                cancelled = await cancel_run(
                    database,
                    run_id=first.id,
                    actor_user_id=user.id,
                    request_id="cancel",
                )
            assert cancelled is not None and cancelled.status == "cancelled"
            async with data_management_database() as database:
                await enqueue_run(
                    database,
                    operation="macro_dashboard",
                    market_code=None,
                    requester_id=user.id,
                    request_id="second",
                )
            claimed_second = await claim_next_run(data_management_database, "worker-two")
            assert claimed_second is not None and claimed_second.id != first.id
            async with data_management_database() as contender:
                assert not await contender.scalar(
                    select(func.pg_try_advisory_lock(MACRO_EXECUTION_LOCK_KEY))
                )
        finally:
            await execution_session.execute(
                select(func.pg_advisory_unlock(MACRO_EXECUTION_LOCK_KEY))
            )
            await execution_session.rollback()


async def test_claim_recovers_expired_lease_heartbeats_and_owner_guards_completion(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(data_management_database)
    async with data_management_database() as database:
        queued = await enqueue_run(
            database,
            operation="index_yahoo",
            market_code=None,
            requester_id=user.id,
            request_id="a",
        )
    claimed = await claim_next_run(data_management_database, "worker-a")
    assert claimed is not None and claimed.id == queued.id
    assert await heartbeat_run(data_management_database, claimed.id, "worker-a")
    assert not await heartbeat_run(data_management_database, claimed.id, "worker-b")
    async with data_management_database.begin() as database:
        await database.execute(
            update(DataManagementRun)
            .where(DataManagementRun.id == claimed.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    recovered = await claim_next_run(data_management_database, "worker-b")
    assert recovered is not None and recovered.id == claimed.id
    await complete_run(
        data_management_database, recovered, "worker-a", status="succeeded", result={}
    )
    async with data_management_database() as database:
        still_running = await database.get(DataManagementRun, claimed.id)
    assert still_running is not None and still_running.status == "running"
    await complete_run(
        data_management_database, recovered, "worker-b", status="succeeded", result={}
    )
    async with data_management_database() as database:
        completed = await database.get(DataManagementRun, claimed.id)
    assert completed is not None and completed.status == "succeeded"


async def test_expired_live_execution_lock_is_not_reclaimed_until_worker_session_releases(
    data_management_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _admin(data_management_database)
    async with data_management_database() as database:
        queued = await enqueue_run(
            database,
            operation="index_yahoo",
            market_code=None,
            requester_id=user.id,
            request_id="execution-fence",
        )
    claimed = await claim_next_run(data_management_database, "worker-a")
    assert claimed is not None
    async with data_management_database.begin() as database:
        await database.execute(
            update(DataManagementRun)
            .where(DataManagementRun.id == queued.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    live_worker_session = data_management_database()
    await live_worker_session.execute(select(func.pg_advisory_lock(execution_lock_key(queued.id))))
    provider_calls: list[uuid.UUID] = []

    async def provider_call(
        run: DataManagementRun, *_: object
    ) -> tuple[str, dict[str, object], str | None]:
        provider_calls.append(run.id)
        return "succeeded", {}, None

    monkeypatch.setattr(data_management_service, "execute_run", provider_call)
    try:
        # The expired DB lease is insufficient to start worker B while A's
        # execution session can still be in a provider call.
        await worker_loop(data_management_database, Settings(environment="test"), once=True)
        assert provider_calls == []
    finally:
        await live_worker_session.execute(
            select(func.pg_advisory_unlock(execution_lock_key(queued.id)))
        )
        await live_worker_session.close()

    await worker_loop(data_management_database, Settings(environment="test"), once=True)
    assert provider_calls == [queued.id]


async def test_admin_api_enqueues_lists_gets_conflicts_and_audits(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(data_management_database)
    async with _admin_client(data_management_database, user, enabled=True) as client:
        catalog = await client.get("/api/admin/data-management/catalog")
        created = await client.post(
            "/api/admin/data-management/runs", json={"operation": "morning_all"}
        )
        listed = await client.get("/api/admin/data-management/runs")
        duplicate = await client.post(
            "/api/admin/data-management/runs",
            json={"operation": "morning_market", "market_code": "crypto"},
        )
        macro = await client.post(
            "/api/admin/data-management/runs",
            json={"operation": "morning_market", "market_code": "global_macro_bonds"},
        )
        fetched = await client.get(f"/api/admin/data-management/runs/{created.json()['id']}")

    assert catalog.status_code == 200
    assert created.status_code == 202, created.text
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    assert duplicate.status_code == 409
    assert macro.status_code == 202
    assert macro.json()["operation"] == "macro_dashboard"
    assert macro.json()["market_code"] is None
    assert fetched.status_code == 200 and fetched.json()["operation"] == "morning_all"
    async with data_management_database() as database:
        actions = list(
            await database.scalars(
                select(AuditEvent.action).where(AuditEvent.target_id == created.json()["id"])
            )
        )
    assert actions == ["data_management.run_enqueued"]


async def test_admin_api_filters_news_runs_before_applying_limit(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(data_management_database)
    newest = datetime.now(UTC)
    async with data_management_database.begin() as database:
        database.add_all(
            [
                DataManagementRun(
                    operation="morning_all",
                    market_code=None,
                    edition_date=newest.date(),
                    status="succeeded",
                    requested_by_user_id=user.id,
                    created_at=newest - timedelta(minutes=index),
                )
                for index in range(20)
            ]
            + [
                DataManagementRun(
                    operation="news_all",
                    market_code=None,
                    edition_date=newest.date(),
                    status="pending",
                    requested_by_user_id=user.id,
                    created_at=newest - timedelta(minutes=21),
                )
            ]
        )

    async with _admin_client(data_management_database, user, enabled=True) as client:
        all_runs = await client.get("/api/admin/data-management/runs?limit=20")
        news_runs = await client.get("/api/admin/data-management/runs?limit=1&operation_group=news")
        invalid_filter = await client.get("/api/admin/data-management/runs?operation_group=morning")

    assert all(run["operation"] != "news_all" for run in all_runs.json()["items"])
    assert news_runs.status_code == 200
    assert [run["operation"] for run in news_runs.json()["items"]] == ["news_all"]
    assert invalid_filter.status_code == 422


async def test_admin_api_rejects_unauthenticated_writes_and_disabled_providers(
    data_management_database: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(
        Settings(environment="test"), readiness(True), session_factory=data_management_database
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthenticated = await client.post(
            "/api/admin/data-management/runs", json={"operation": "morning_all"}
        )
    user = await _admin(data_management_database)
    async with _admin_client(data_management_database, user, enabled=False) as client:
        unavailable = await client.post(
            "/api/admin/data-management/runs", json={"operation": "index_yahoo"}
        )
    assert unauthenticated.status_code == 401
    assert unavailable.status_code == 503
