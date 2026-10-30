import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from anyio import Path as AsyncPath
from conftest import remigrate_database
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_health import readiness

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.core.security import hash_password
from daily_insights_api.modules.audit.models import AuditEvent
from daily_insights_api.modules.data_sources.api import TwelveDataAdapter
from daily_insights_api.modules.identity.api import (
    AuthContext,
    require_csrf,
    require_password_changed,
)
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.modules.orchestration.models import DataManagementRun, JobRun
from daily_insights_api.scripts import check_legacy_queues_quiescent as legacy_queues
from daily_insights_api.scripts import run_morning_reports
from daily_insights_api.scripts.run_morning_reports import TWELVE_DATA_EXECUTION_LOCK_KEY
from daily_insights_api.web.app import create_app

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def orchestration_admin_database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.environ.get("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    remigrate_database(database_url)
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory.begin() as database:
        database.add(
            User(
                email="orchestration@example.com",
                display_name="Orchestration Admin",
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
        user = await database.scalar(select(User).where(User.email == "orchestration@example.com"))
    assert user is not None
    return user


def _admin_client(
    factory: async_sessionmaker[AsyncSession],
    user: User,
    *,
    enabled: bool,
    analyst_viewpoints_enabled: bool = False,
) -> AsyncClient:
    app = create_app(
        Settings(
            environment="test",
            morning_reports_enabled=enabled,
            twelve_data_api_key="test-key",
            yfinance_enabled=enabled,
            # The index run covers Yahoo and TWSE, so "no provider enabled"
            # now means both are off.
            twse_enabled=enabled,
            analyst_viewpoints_enabled=analyst_viewpoints_enabled,
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


async def test_scheduled_morning_waits_for_provider_execution_lock(
    orchestration_admin_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider_started = asyncio.Event()

    async def missing(*_: object) -> tuple[str, ...]:
        return ("crypto",)

    async def execute(*_: object) -> tuple[()]:
        provider_started.set()
        return ()

    monkeypatch.setattr(run_morning_reports, "unpublished_morning_report_markets", missing)
    monkeypatch.setattr(run_morning_reports, "run_scheduled_morning_report_markets", execute)
    heartbeat = AsyncPath(str(tmp_path) + "/morning-heartbeat")
    lock_session = orchestration_admin_database()
    await lock_session.execute(select(func.pg_advisory_lock(TWELVE_DATA_EXECUTION_LOCK_KEY)))
    scheduled = asyncio.create_task(
        run_morning_reports.run_scheduled_morning_report_edition(
            orchestration_admin_database,
            cast(TwelveDataAdapter, object()),
            date(2026, 9, 15),
            heartbeat,
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert not provider_started.is_set()
    finally:
        await lock_session.execute(select(func.pg_advisory_unlock(TWELVE_DATA_EXECUTION_LOCK_KEY)))
        await lock_session.rollback()
        await lock_session.close()
    await asyncio.wait_for(scheduled, timeout=5)
    assert provider_started.is_set()


async def test_orchestration_admin_api_enqueues_lists_gets_and_queues_conflicts(
    orchestration_admin_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(orchestration_admin_database)
    async with _admin_client(
        orchestration_admin_database,
        user,
        enabled=True,
        analyst_viewpoints_enabled=True,
    ) as client:
        catalog = await client.get("/api/admin/orchestration/catalog")
        created = await client.post(
            "/api/admin/orchestration/job-runs", json={"job_key": "global_macro_refresh"}
        )
        listed = await client.get("/api/admin/orchestration/job-runs")
        invalid = await client.post(
            "/api/admin/orchestration/job-runs", json={"job_key": "twelve_data_daily_update"}
        )
        removed_news = await client.post(
            "/api/admin/orchestration/job-runs", json={"job_key": "news_daily_update"}
        )
        taiwan = await client.post(
            "/api/admin/orchestration/job-runs", json={"job_key": "tw_equity_refresh"}
        )
        fetched = await client.get(f"/api/admin/orchestration/job-runs/{created.json()['id']}")

    assert catalog.status_code == 200
    assert catalog.json()["features"] == {"analyst_viewpoints": True}
    assert not any(job["key"].startswith("news_") for job in catalog.json()["jobs"])
    assert created.status_code == 202, created.text
    assert listed.status_code == 200
    assert {item["job_key"] for item in listed.json()["items"]} == {
        "global_macro_refresh",
        "market_reports_publish",
        "macro_dashboard_publish",
    }
    assert invalid.status_code == 422
    assert removed_news.status_code == 422
    assert taiwan.status_code == 202, taiwan.text
    assert fetched.status_code == 200 and fetched.json()["job_key"] == "global_macro_refresh"
    assert len(fetched.json()["functions"]) == 6
    async with orchestration_admin_database() as database:
        manual_runs = list(
            (
                await database.scalars(
                    select(JobRun).where(
                        or_(
                            JobRun.id == uuid.UUID(created.json()["id"]),
                            JobRun.payload["source_job_run_id"].astext == created.json()["id"],
                        )
                    )
                )
            ).all()
        )
    assert len(manual_runs) == 3
    deadlines = {run.deadline_at for run in manual_runs}
    assert None not in deadlines
    assert len(deadlines) == 1
    deadline = deadlines.pop()
    assert deadline is not None
    assert timedelta(minutes=59) <= deadline - datetime.now(UTC) <= timedelta(hours=1)
    async with orchestration_admin_database() as database:
        created_event = await database.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "orchestration.job_created",
                AuditEvent.target_id == created.json()["id"],
            )
        )
    assert created_event is not None
    assert created_event.actor_user_id == user.id
    assert created_event.request_id is not None
    assert created_event.before is None
    assert created_event.after == {
        "job_key": "global_macro_refresh",
        "edition_date": created.json()["edition_date"],
        "status": "pending",
        "trigger": "manual",
    }


async def test_orchestration_cancel_records_atomic_audit_event(
    orchestration_admin_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(orchestration_admin_database)
    async with _admin_client(orchestration_admin_database, user, enabled=True) as client:
        created = await client.post(
            "/api/admin/orchestration/job-runs",
            json={"job_key": "tw_equity_refresh"},
        )
        assert created.status_code == 202, created.text
        cancelled = await client.post(
            f"/api/admin/orchestration/job-runs/{created.json()['id']}/cancel"
        )

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    async with orchestration_admin_database() as database:
        event = await database.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "orchestration.job_cancelled",
                AuditEvent.target_id == created.json()["id"],
            )
        )
    assert event is not None
    assert event.actor_user_id == user.id
    assert event.request_id is not None
    assert event.before == {"status": "pending"}
    assert event.after == {"status": "cancelled", "job_key": "tw_equity_refresh"}


async def test_orchestration_api_paginates_and_filters_job_runs_before_pagination(
    orchestration_admin_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(orchestration_admin_database)
    newest = datetime.now(UTC)
    async with orchestration_admin_database.begin() as database:
        database.add_all(
            [
                JobRun(
                    job_key="global_macro_refresh",
                    kind="function",
                    trigger="manual",
                    registry_version="test",
                    registry_snapshot={},
                    edition_date=newest.date(),
                    status="succeeded",
                    requested_by_user_id=user.id,
                    created_at=newest - timedelta(minutes=index),
                )
                for index in range(20)
            ]
            + [
                JobRun(
                    job_key="news_publish_job",
                    kind="function",
                    trigger="manual",
                    registry_version="test",
                    registry_snapshot={},
                    edition_date=newest.date(),
                    status="pending",
                    requested_by_user_id=user.id,
                    created_at=newest - timedelta(minutes=21),
                )
            ]
        )

    async with _admin_client(orchestration_admin_database, user, enabled=True) as client:
        all_runs = await client.get("/api/admin/orchestration/job-runs?page=1")
        last_page = await client.get("/api/admin/orchestration/job-runs?page=3")
        invalid_page = await client.get("/api/admin/orchestration/job-runs?page=0")

    assert all_runs.status_code == 200
    assert len(all_runs.json()["items"]) == 10
    assert all_runs.json()["page"] == 1
    assert all_runs.json()["page_size"] == 10
    assert all_runs.json()["total"] == 21
    assert all(run["job_key"] != "news_publish_job" for run in all_runs.json()["items"])
    # Runs of jobs since removed from the registry stay listed as history.
    assert [run["job_key"] for run in last_page.json()["items"]] == ["news_publish_job"]
    assert last_page.json()["has_more"] is False
    assert invalid_page.status_code == 422


async def test_legacy_create_run_contract_is_removed(
    orchestration_admin_database: async_sessionmaker[AsyncSession],
) -> None:
    user = await _admin(orchestration_admin_database)
    async with _admin_client(orchestration_admin_database, user, enabled=True) as client:
        refused = await client.post(
            "/api/admin/data-management/runs", json={"operation": "news_publish"}
        )
    assert refused.status_code == 404


async def test_legacy_archive_preserves_response_pagination_and_quiescence_check(
    orchestration_admin_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _admin(orchestration_admin_database)
    settings = Settings(
        environment="test", database_url=os.environ["DAILY_INSIGHTS_TEST_DATABASE_URL"]
    )
    monkeypatch.setattr(legacy_queues, "Settings", lambda: settings)
    assert await legacy_queues._main() == 0
    created_at = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
    rows = [
        DataManagementRun(
            id=uuid.UUID(int=index + 1),
            operation="news_market",
            market_code="global",
            edition_date=created_at.date(),
            status="pending" if index == 0 else "succeeded",
            requested_by_user_id=user.id,
            started_at=None if index == 0 else created_at,
            completed_at=None if index == 0 else created_at,
            result={"historical": {"count": index}},
            error=None,
            created_at=created_at,
        )
        for index in range(12)
    ]
    async with orchestration_admin_database.begin() as database:
        # Seed pre-cutover history only in this disposable database, then
        # restore the production trigger before exercising either read path.
        await database.execute(
            text(
                "ALTER TABLE legacy_data_management_runs "
                "DISABLE TRIGGER legacy_data_management_runs_are_read_only"
            )
        )
        database.add_all(rows)
        await database.flush()
        await database.execute(
            text(
                "ALTER TABLE legacy_data_management_runs "
                "ENABLE TRIGGER legacy_data_management_runs_are_read_only"
            )
        )

    async with _admin_client(orchestration_admin_database, user, enabled=True) as client:
        first = await client.get("/api/admin/orchestration/legacy-runs")
        second = await client.get("/api/admin/orchestration/legacy-runs?page=2")
        empty = await client.get("/api/admin/orchestration/legacy-runs?page=3")
        invalid = await client.get("/api/admin/orchestration/legacy-runs?page=0")

    assert first.status_code == second.status_code == empty.status_code == 200
    expected = [
        {
            "id": str(row.id),
            "operation": "news_market",
            "market_code": "global",
            "edition_date": "2026-09-15",
            "status": row.status,
            "requested_by_user_id": str(user.id),
            "started_at": "2026-09-15T00:00:00Z" if row.started_at else None,
            "completed_at": "2026-09-15T00:00:00Z" if row.completed_at else None,
            "result": row.result,
            "error": None,
            "created_at": "2026-09-15T00:00:00Z",
        }
        for row in reversed(rows)
    ]
    for page, response in enumerate((first, second, empty), start=1):
        assert response.json() == {
            "items": expected[(page - 1) * 10 : page * 10],
            "page": page,
            "page_size": 10,
            "total": 12,
            "has_more": page == 1,
        }
    assert invalid.status_code == 422
    assert await legacy_queues._main() == legacy_queues.ACTIVE_LEGACY_QUEUE


async def test_orchestration_admin_api_rejects_unauthenticated_and_non_manual_jobs(
    orchestration_admin_database: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(
        Settings(environment="test"), readiness(True), session_factory=orchestration_admin_database
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthenticated = await client.post(
            "/api/admin/orchestration/job-runs", json={"job_key": "global_macro_refresh"}
        )
    user = await _admin(orchestration_admin_database)
    async with _admin_client(orchestration_admin_database, user, enabled=False) as client:
        refused = await client.post(
            "/api/admin/orchestration/job-runs",
            json={"job_key": "yahoo_finance_daily_update"},
        )
    assert unauthenticated.status_code == 401
    assert refused.status_code == 422
