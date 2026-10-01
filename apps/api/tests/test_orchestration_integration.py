import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
import pytest_asyncio
from conftest import remigrate_database
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import daily_insights_api.modules.orchestration.functions as orchestration_functions
import daily_insights_api.modules.orchestration.projections as orchestration_projections
from daily_insights_api.core.config import Settings
from daily_insights_api.modules.analyst_viewpoints.api import AnalystViewpointSyncError
from daily_insights_api.modules.analyst_viewpoints.models import AnalystViewpointSyncRun
from daily_insights_api.modules.data_sources.api import TaiexDailyBar, TaiexDailyBars
from daily_insights_api.modules.orchestration.models import (
    FunctionAttempt,
    FunctionRun,
    JobDependency,
    JobRun,
    MarketDailyObservation,
    MarketDailySeries,
    ProjectionInputFreeze,
    PublicationFunctionAttempt,
    RoutineRun,
)
from daily_insights_api.modules.orchestration.projections import (
    FrozenObservation,
    _heartbeat_projection,
    claim_ready_projection,
    execute_projection,
    freeze_projection_inputs,
    publish_macro_dashboard,
    publish_market_reports,
)
from daily_insights_api.modules.orchestration.router import _routine_responses
from daily_insights_api.modules.orchestration.service import (
    RECONCILIATION_BATCH_SIZE,
    cancel_job_run,
    create_daily_routine,
    terminalize_expired_automatic_functions,
)
from daily_insights_api.modules.orchestration.worker import (
    AttemptStatus,
    ClaimedFunction,
    FunctionOutcome,
    _ready_candidates,
    aggregate_routines,
    claim_ready_function,
    execute_claimed,
    reconcile_function_jobs,
)
from daily_insights_api.modules.reports.api import TENORS, History, Point
from daily_insights_api.modules.reports.macro_dashboard_models import MacroDashboardSnapshot
from daily_insights_api.modules.reports.macro_diagnostics import record_failure
from daily_insights_api.modules.reports.models import ReportPublication

pytestmark = pytest.mark.integration


def _complete_treasury_coverage(*, through: date) -> dict[tuple[int, int], set[str]]:
    symbols = {symbol for _, symbol in TENORS}
    return {
        (year, month): symbols
        for year in range(through.year - 2, through.year + 1)
        for month in range(1, 13 if year < through.year else through.month + 1)
    }


def test_treasury_fetch_periods_uses_only_current_month_when_coverage_is_complete() -> None:
    today = date(2026, 9, 17)

    assert orchestration_functions._treasury_fetch_periods(
        today, _complete_treasury_coverage(through=today)
    ) == ((), ((2026, 9),))


def test_treasury_fetch_periods_refetches_latest_stored_month_across_month_boundary() -> None:
    # 2026-10-01 08:00 Taipei is still 2026-09-30 in New York, so October has
    # no rows yet and the September month-end close must be refetched.
    assert orchestration_functions._treasury_fetch_periods(
        date(2026, 10, 1), _complete_treasury_coverage(through=date(2026, 9, 30))
    ) == ((), ((2026, 9), (2026, 10)))
    # 2026-11-01 is a Sunday: the Monday run still reads October alongside
    # the empty November feed.
    assert orchestration_functions._treasury_fetch_periods(
        date(2026, 11, 2), _complete_treasury_coverage(through=date(2026, 10, 31))
    ) == ((), ((2026, 10), (2026, 11)))


def test_treasury_fetch_periods_backfills_missing_year_and_new_year_baseline() -> None:
    symbols = {symbol for _, symbol in TENORS}
    today = date(2026, 9, 17)
    coverage = _complete_treasury_coverage(through=today)
    coverage.pop((2025, 6))

    assert orchestration_functions._treasury_fetch_periods(today, coverage) == (
        (2025,),
        ((2026, 9),),
    )
    assert orchestration_functions._treasury_fetch_periods(
        date(2026, 1, 1),
        {(year, month): symbols for year in (2024, 2025) for month in range(1, 13)},
    ) == ((), ((2025, 12), (2026, 1)))


@pytest_asyncio.fixture
async def orchestration_database() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker[AsyncSession]]
]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    remigrate_database(database_url)
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, sessions
    finally:
        await engine.dispose()


async def test_legacy_data_management_archive_rejects_writes(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, _ = orchestration_database
    async with engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT count(*) FROM legacy_data_management_runs")) == 0
        )
    with pytest.raises(DBAPIError, match="orchestration facts and provenance are immutable"):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO legacy_data_management_runs "
                    "(id, operation, edition_date, status) "
                    "VALUES (:id, 'morning_all', :edition_date, 'pending')"
                ),
                {"id": uuid.uuid4(), "edition_date": date(2026, 9, 17)},
            )


async def test_taiex_missing_month_retry_keeps_latest_stored_source_date(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = orchestration_database
    latest = date(2026, 9, 16)
    old_month = date(2025, 2, 1)
    token = uuid.uuid4()

    async def latest_date(*_: object, **__: object) -> date:
        return latest

    async def select_months(*_: object, **__: object) -> tuple[date, ...]:
        return (old_month,)

    async def store(*_: object, **__: object) -> int:
        return 1

    async def current(*_: object, **__: object) -> bool:
        return True

    class Adapter:
        async def get_taiex_daily_bars(self, month: date) -> TaiexDailyBars:
            assert month == old_month
            return TaiexDailyBars(
                month=month,
                items=(
                    TaiexDailyBar(
                        trade_date=date(2025, 2, 27),
                        open=Decimal("23000"),
                        high=Decimal("23100"),
                        low=Decimal("22900"),
                        close=Decimal("23050"),
                        volume=1,
                        trade_value=1,
                    ),
                ),
                fetched_at=datetime(2026, 9, 17, tzinfo=UTC),
            )

    monkeypatch.setattr(orchestration_functions, "latest_market_date", latest_date)
    monkeypatch.setattr(orchestration_functions, "select_taiex_refresh_months", select_months)
    monkeypatch.setattr(orchestration_functions, "store_market_bars", store)
    monkeypatch.setattr(orchestration_functions, "store_index_daily_bars", store)
    monkeypatch.setattr(orchestration_functions, "fence_is_current", current)
    claimed = ClaimedFunction(
        connection=cast(AsyncConnection, None),
        function_run_id=uuid.uuid4(),
        job_run_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        function_key="taiex_daily_bars",
        provider_key="twse",
        edition_date=date(2026, 9, 17),
        fence_token=token,
        deadline_at=None,
        scope={"missing_scopes": [old_month.isoformat()]},
    )

    outcome = await orchestration_functions._run_twse_taiex(
        Settings(environment="test", twse_enabled=True),
        sessions,
        claimed,
        cast(Any, Adapter()),
    )

    assert outcome.status == "succeeded"
    assert outcome.source_as_of == latest


async def test_treasury_failed_year_is_partial_and_retryable(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = orchestration_database
    current_year = date(2026, 9, 17).year

    async def load(*_: object, **__: object) -> list[History]:
        record_failure(
            "us_treasury",
            "yield_curve_xml",
            [str(current_year)],
            TimeoutError("current year timed out"),
        )
        return [
            History(
                id=tenor,
                symbol=symbol,
                unit="percent",
                source="U.S. Treasury",
                status="ok",
                points=[Point(date=date(2025, 12, 31), value=Decimal("4.0"))],
            )
            for tenor, symbol in TENORS
        ]

    async def store(*_: object, **__: object) -> int:
        return 1

    monkeypatch.setattr(orchestration_functions, "load_treasury", load)
    monkeypatch.setattr(orchestration_functions, "store_interest_rates", store)
    claimed = ClaimedFunction(
        connection=cast(AsyncConnection, None),
        function_run_id=uuid.uuid4(),
        job_run_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        function_key="treasury_yield_curve",
        provider_key="us_treasury",
        edition_date=date(2026, 9, 17),
        fence_token=uuid.uuid4(),
        deadline_at=None,
        scope={},
    )

    outcome = await orchestration_functions._run_treasury(
        Settings(environment="test"), sessions, claimed
    )

    assert outcome.status == "partial"
    assert outcome.retryable is True
    assert str(current_year) in outcome.missing_scopes
    assert outcome.result is not None
    assert outcome.result["failure_reasons"] == {str(current_year): "timeout"}


async def test_daily_routine_is_idempotent_and_worker_persists_attempts(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, sessions = orchestration_database
    # A future edition, so the routine deadline has not terminalized its jobs.
    edition = datetime.now(UTC).date() + timedelta(days=2)
    async with sessions() as database:
        first = await create_daily_routine(database, edition_date=edition)
    async with sessions() as database:
        second = await create_daily_routine(database, edition_date=edition)

    assert first.id == second.id
    async with sessions() as database:
        assert await database.scalar(select(func.count()).select_from(RoutineRun)) == 1
        assert await database.scalar(select(func.count()).select_from(JobRun)) == 9
        assert await database.scalar(select(func.count()).select_from(JobDependency)) == 8

    claimed = await claim_ready_function(engine, sessions, owner="integration-worker")
    assert claimed is not None

    async def succeed(_: ClaimedFunction) -> FunctionOutcome:
        return FunctionOutcome(
            status="succeeded",
            source_as_of=edition,
            record_count=1,
            payload_digest="a" * 64,
        )

    await execute_claimed(claimed, sessions, succeed)
    async with sessions() as database:
        function = await database.get(FunctionRun, claimed.function_run_id)
        attempt = await database.get(FunctionAttempt, claimed.attempt_id)
        assert function is not None and function.status == "succeeded"
        assert attempt is not None and attempt.status == "succeeded"
        assert attempt.source_as_of == edition
        assert attempt.record_count == 1
        assert attempt.fence_token == claimed.fence_token
        assert (
            await database.scalar(
                select(func.count())
                .select_from(FunctionAttempt)
                .where(FunctionAttempt.function_run_id == claimed.function_run_id)
            )
            == 1
        )


async def test_routine_response_uses_bounded_query_count(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, sessions = orchestration_database
    async with sessions() as database:
        routine = await create_daily_routine(database, edition_date=date(2026, 9, 17))
    statement_count = 0

    def count_statement(*_: object) -> None:
        nonlocal statement_count
        statement_count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        async with sessions() as database:
            responses = await _routine_responses(database, [routine])
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_statement)

    assert len(responses) == 1
    assert len(responses[0].jobs) == 9
    assert statement_count <= 5


async def test_cancellation_fences_a_running_function_and_attempt(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, sessions = orchestration_database
    async with sessions() as database:
        # A future edition, so the routine deadline has not terminalized its jobs.
        await create_daily_routine(
            database, edition_date=datetime.now(UTC).date() + timedelta(days=2)
        )
    claimed = await claim_ready_function(engine, sessions, owner="cancelled-worker")
    assert claimed is not None

    async with sessions() as database:
        cancellation = await cancel_job_run(database, job_run_id=claimed.job_run_id)
        await database.commit()
    assert cancellation is not None
    cancelled, previous_status = cancellation
    assert cancelled.status == "cancelled"
    assert previous_status == "running"

    async def stale_success(_: ClaimedFunction) -> FunctionOutcome:
        return FunctionOutcome(status="succeeded", record_count=1)

    await execute_claimed(claimed, sessions, stale_success)
    async with sessions() as database:
        function = await database.get(FunctionRun, claimed.function_run_id)
        attempt = await database.get(FunctionAttempt, claimed.attempt_id)
        job = await database.get(JobRun, claimed.job_run_id)
        assert function is not None and function.status == "cancelled"
        assert function.lease_token is None
        assert attempt is not None and attempt.status == "cancelled"
        assert attempt.record_count is None
        assert job is not None and job.status == "cancelled"


async def test_registry_version_change_does_not_duplicate_daily_routine(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = orchestration_database
    edition = date(2026, 9, 17)
    async with sessions() as database:
        first = await create_daily_routine(database, edition_date=edition)
    async with sessions.begin() as database:
        stored = await database.get(RoutineRun, first.id)
        assert stored is not None
        stored.registry_version = "previous-registry-version"
    async with sessions() as database:
        second = await create_daily_routine(database, edition_date=edition)

    assert second.id == first.id
    async with sessions() as database:
        assert await database.scalar(select(func.count()).select_from(RoutineRun)) == 1
        assert await database.scalar(select(func.count()).select_from(JobRun)) == 9


async def test_projection_fence_token_rejects_stale_worker_with_same_owner(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = orchestration_database
    edition = datetime.now(UTC).date() + timedelta(days=2)
    async with sessions() as database:
        await create_daily_routine(database, edition_date=edition)
    async with sessions.begin() as database:
        projection = await database.scalar(
            select(JobRun).where(JobRun.job_key == "market_reports_publish")
        )
        assert projection is not None
        upstream_ids = select(JobDependency.upstream_job_run_id).where(
            JobDependency.downstream_job_run_id == projection.id
        )
        await database.execute(
            update(JobRun)
            .where(JobRun.id.in_(upstream_ids))
            .values(status="succeeded", completed_at=datetime.now(UTC))
        )

    first = await claim_ready_projection(sessions, owner="projection-worker")
    assert first is not None
    async with sessions.begin() as database:
        job = await database.get(JobRun, first.job_run_id)
        assert job is not None
        job.lease_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
    second = await claim_ready_projection(sessions, owner="projection-worker")
    assert second is not None
    assert second.job_run_id == first.job_run_id
    assert second.fence_token != first.fence_token
    assert not await _heartbeat_projection(
        sessions,
        job_run_id=first.job_run_id,
        owner="projection-worker",
        fence_token=first.fence_token,
    )
    assert await _heartbeat_projection(
        sessions,
        job_run_id=second.job_run_id,
        owner="projection-worker",
        fence_token=second.fence_token,
    )


async def test_projection_transient_failure_retries_same_job(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = orchestration_database
    now = datetime.now(UTC)
    async with sessions.begin() as database:
        job = JobRun(
            job_key="market_reports_publish",
            kind="projection",
            trigger="manual",
            registry_version="test",
            registry_snapshot={},
            edition_date=now.date(),
            deadline_at=now + timedelta(hours=1),
            status="pending",
        )
        database.add(job)
        await database.flush()
        job_id = job.id

    freeze_calls = 0
    publish_calls = 0

    async def freeze(
        *_: object, **__: object
    ) -> tuple[orchestration_projections.FrozenObservation, ...]:
        nonlocal freeze_calls
        freeze_calls += 1
        if freeze_calls == 1:
            raise ConnectionError("temporary database disconnect")
        return ()

    async def publish(*_: object, **__: object) -> dict[str, object]:
        nonlocal publish_calls
        publish_calls += 1
        return {"partial": False}

    monkeypatch.setattr(orchestration_projections, "freeze_projection_inputs", freeze)
    monkeypatch.setattr(orchestration_projections, "publish_market_reports", publish)

    first = await claim_ready_projection(sessions, owner="projection-retry-worker")
    assert first is not None
    await execute_projection(
        sessions,
        job_run_id=first.job_run_id,
        owner="projection-retry-worker",
        fence_token=first.fence_token,
    )
    async with sessions.begin() as database:
        stored = await database.get(JobRun, job_id)
        assert stored is not None
        assert stored.status == "pending"
        assert stored.next_attempt_at is not None
        stored.next_attempt_at = now - timedelta(seconds=1)

    second = await claim_ready_projection(sessions, owner="projection-retry-worker")
    assert second is not None and second.job_run_id == job_id
    await execute_projection(
        sessions,
        job_run_id=second.job_run_id,
        owner="projection-retry-worker",
        fence_token=second.fence_token,
    )
    async with sessions() as database:
        stored = await database.get(JobRun, job_id)
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.next_attempt_at is None
    assert freeze_calls == 2
    assert publish_calls == 1


async def test_projection_failure_after_deadline_is_terminal(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = orchestration_database
    now = datetime.now(UTC)
    async with sessions.begin() as database:
        job = JobRun(
            job_key="market_reports_publish",
            kind="projection",
            trigger="manual",
            registry_version="test",
            registry_snapshot={},
            edition_date=now.date(),
            deadline_at=now - timedelta(seconds=1),
            status="pending",
        )
        database.add(job)
        await database.flush()
        job_id = job.id

    async def fail(*_: object, **__: object) -> tuple[FrozenObservation, ...]:
        raise ConnectionError("temporary database disconnect")

    monkeypatch.setattr(orchestration_projections, "freeze_projection_inputs", fail)

    claimed = await claim_ready_projection(sessions, owner="projection-deadline-worker")
    assert claimed is not None
    await execute_projection(
        sessions,
        job_run_id=claimed.job_run_id,
        owner="projection-deadline-worker",
        fence_token=claimed.fence_token,
    )

    async with sessions() as database:
        stored = await database.get(JobRun, job_id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.next_attempt_at is None
        assert stored.completed_at is not None


async def test_older_macro_projection_cannot_overwrite_newer_freeze(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = orchestration_database
    now = datetime.now(UTC)
    edition = now.date()
    jobs: list[tuple[uuid.UUID, str, uuid.UUID]] = []
    async with sessions.begin() as database:
        for suffix, cutoff in (("older", now - timedelta(hours=1)), ("newer", now)):
            owner = f"macro-{suffix}"
            token = uuid.uuid4()
            job = JobRun(
                job_key="macro_dashboard_publish",
                kind="projection",
                trigger="manual",
                registry_version="test",
                registry_snapshot={},
                edition_date=edition,
                deadline_at=now + timedelta(hours=1),
                status="running",
                lease_owner=owner,
                lease_token=token,
                lease_expires_at=now + timedelta(minutes=10),
            )
            database.add(job)
            await database.flush()
            database.add(
                ProjectionInputFreeze(
                    projection_job_run_id=job.id,
                    registry_version="test",
                    cutoff_at=cutoff,
                    input_digest=("1" if suffix == "older" else "2") * 64,
                    inputs={"observations": [], "function_outcomes": []},
                )
            )
            jobs.append((job.id, owner, token))

    older_job, older_owner, older_token = jobs[0]
    newer_job, newer_owner, newer_token = jobs[1]

    def observation(value: str, digest: str) -> tuple[FrozenObservation, ...]:
        return (
            FrozenObservation(
                kind="market",
                id=uuid.uuid4(),
                function_attempt_id=uuid.uuid4(),
                provider_key="twelve_data",
                dataset_key="commodity_daily_bars",
                symbol="WTI/USD",
                unit="usd",
                observation_date=edition,
                value=Decimal(value),
                open_value=Decimal(value),
                value_digest=digest * 64,
            ),
        )

    newer_result = await publish_macro_dashboard(
        sessions,
        job_run_id=newer_job,
        owner=newer_owner,
        fence_token=newer_token,
        frozen=observation("2", "2"),
    )
    older_result = await publish_macro_dashboard(
        sessions,
        job_run_id=older_job,
        owner=older_owner,
        fence_token=older_token,
        frozen=observation("1", "1"),
    )

    assert newer_result["action"] == "published"
    assert older_result["action"] == "superseded"
    async with sessions() as database:
        snapshot = await database.get(MacroDashboardSnapshot, "global_macro_bonds")
        assert snapshot is not None
        assert snapshot.projection_job_run_id == newer_job
        assert snapshot.edition_date == edition


async def test_older_market_report_projection_cannot_create_newer_revision(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = orchestration_database
    now = datetime.now(UTC)
    jobs: list[tuple[uuid.UUID, str, uuid.UUID]] = []
    async with sessions.begin() as database:
        for suffix, cutoff in (("older", now - timedelta(hours=1)), ("newer", now)):
            owner = f"report-{suffix}"
            token = uuid.uuid4()
            job = JobRun(
                job_key="market_reports_publish",
                kind="projection",
                trigger="manual",
                registry_version="test",
                registry_snapshot={},
                edition_date=now.date(),
                status="running",
                payload={"requested_market_job": "us_equity_refresh"},
                lease_owner=owner,
                lease_token=token,
                lease_expires_at=now + timedelta(minutes=10),
            )
            database.add(job)
            await database.flush()
            database.add(
                ProjectionInputFreeze(
                    projection_job_run_id=job.id,
                    registry_version="test",
                    cutoff_at=cutoff,
                    input_digest=("3" if suffix == "older" else "4") * 64,
                    inputs={
                        "observations": [],
                        "function_outcomes": [
                            {
                                "function_key": "us_mega_cap_daily_bars",
                                "status": "failed",
                                "error": suffix,
                                "attempt_ids": [],
                            }
                        ],
                    },
                )
            )
            jobs.append((job.id, owner, token))

    older_job, older_owner, older_token = jobs[0]
    newer_job, newer_owner, newer_token = jobs[1]
    newer_result = await publish_market_reports(
        sessions,
        job_run_id=newer_job,
        owner=newer_owner,
        fence_token=newer_token,
        frozen=(),
    )
    older_result = await publish_market_reports(
        sessions,
        job_run_id=older_job,
        owner=older_owner,
        fence_token=older_token,
        frozen=(),
    )

    assert newer_result["markets"] == {"us_equity": "published"}
    assert older_result["markets"] == {"us_equity": "superseded"}
    async with sessions() as database:
        publications = list(
            await database.scalars(
                select(ReportPublication).where(
                    ReportPublication.market_code == "us_equity",
                    ReportPublication.edition_date == now.date(),
                )
            )
        )
        assert len(publications) == 1
        assert publications[0].projection_job_run_id == newer_job


async def test_projection_degrades_fresh_prior_facts_when_current_provider_failed(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, sessions = orchestration_database
    edition = date(2026, 9, 17)
    owner = "projection-current-outcome"
    fence_token = uuid.uuid4()
    async with sessions() as database:
        await create_daily_routine(database, edition_date=edition)
    async with sessions.begin() as database:
        projection = await database.scalar(
            select(JobRun).where(JobRun.job_key == "market_reports_publish")
        )
        current = await database.scalar(
            select(FunctionRun).where(FunctionRun.function_key == "us_mega_cap_daily_bars")
        )
        assert projection is not None and current is not None
        projection.status = "running"
        projection.lease_owner = owner
        projection.lease_token = fence_token
        projection.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        current.status = "unavailable"
        current.attempt_count = 1
        current.error = "upstream_unavailable"
        failed_attempt = FunctionAttempt(
            function_run_id=current.id,
            attempt_number=1,
            provider_key="twelve_data",
            function_key="us_mega_cap_daily_bars",
            scope={},
            fence_token=uuid.uuid4(),
            status="failed",
            request_metadata=[],
            error_code="upstream_unavailable",
        )
        database.add(failed_attempt)

        prior_job = JobRun(
            job_key="prior_us_data",
            kind="function",
            trigger="manual",
            registry_version="test",
            registry_snapshot={},
            edition_date=edition - timedelta(days=1),
            status="succeeded",
        )
        database.add(prior_job)
        await database.flush()
        prior_function = FunctionRun(
            job_run_id=prior_job.id,
            function_key="us_mega_cap_daily_bars",
            provider_key="twelve_data",
            scope={},
            status="succeeded",
            attempt_count=1,
        )
        database.add(prior_function)
        await database.flush()
        prior_attempt = FunctionAttempt(
            function_run_id=prior_function.id,
            attempt_number=1,
            provider_key="twelve_data",
            function_key="us_mega_cap_daily_bars",
            scope={},
            fence_token=uuid.uuid4(),
            status="succeeded",
            request_metadata=[],
        )
        database.add(prior_attempt)
        await database.flush()
        for symbol in ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA"):
            series = MarketDailySeries(
                provider_key="twelve_data",
                dataset_key="us_mega_cap_daily_bars",
                symbol=symbol,
                market="us_equity",
                unit="usd",
                contract_version="test",
            )
            database.add(series)
            await database.flush()
            for offset, value in ((2, "100"), (1, "101")):
                database.add(
                    MarketDailyObservation(
                        series_id=series.id,
                        function_attempt_id=prior_attempt.id,
                        observation_date=edition - timedelta(days=offset),
                        version=1,
                        close=Decimal(value),
                        value_digest=str(offset) * 64,
                    )
                )
        projection_id = projection.id
        failed_attempt_id = failed_attempt.id

    frozen = await freeze_projection_inputs(
        sessions,
        job_run_id=projection_id,
        owner=owner,
        fence_token=fence_token,
    )
    statement_count = 0

    def count_statement(*_: object) -> None:
        nonlocal statement_count
        statement_count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
    try:
        reloaded = await freeze_projection_inputs(
            sessions,
            job_run_id=projection_id,
            owner=owner,
            fence_token=fence_token,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_statement)
    assert reloaded == frozen
    assert statement_count <= 5
    result = await publish_market_reports(
        sessions,
        job_run_id=projection_id,
        owner=owner,
        fence_token=fence_token,
        frozen=frozen,
    )

    assert result["market_statuses"]["us_equity"] == "unavailable"
    assert result["partial"] is True
    async with sessions() as database:
        publication = await database.scalar(
            select(ReportPublication).where(ReportPublication.market_code == "us_equity")
        )
        assert publication is not None
        linked_attempts = set(
            await database.scalars(
                select(PublicationFunctionAttempt.function_attempt_id).where(
                    PublicationFunctionAttempt.publication_id == publication.id
                )
            )
        )
        assert failed_attempt_id in linked_attempts


async def test_deadline_preserves_partial_result_from_retry_wait(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, sessions = orchestration_database
    # Keep the first attempt before its deadline regardless of the wall-clock
    # date on which the suite runs; the terminalization step advances explicitly.
    edition = datetime.now(UTC).date() + timedelta(days=2)
    async with sessions() as database:
        await create_daily_routine(database, edition_date=edition)
    claimed = await claim_ready_function(engine, sessions, owner="partial-worker")
    assert claimed is not None

    async def partial(_: ClaimedFunction) -> FunctionOutcome:
        return FunctionOutcome(
            status="partial",
            record_count=1,
            result={"symbols": ["AAPL"], "failed_symbols": ["MSFT"]},
            missing_scopes=("MSFT",),
            retryable=True,
        )

    await execute_claimed(claimed, sessions, partial)
    async with sessions() as database:
        function = await database.get(FunctionRun, claimed.function_run_id)
        job = await database.get(JobRun, claimed.job_run_id)
        assert function is not None and function.status == "retry_wait"
        assert function.result == {"symbols": ["AAPL"], "failed_symbols": ["MSFT"]}
        assert job is not None and job.deadline_at is not None
        deadline = job.deadline_at
    async with sessions() as database:
        assert await terminalize_expired_automatic_functions(database, now=deadline) >= 1
    async with sessions() as database:
        function = await database.get(FunctionRun, claimed.function_run_id)
        assert function is not None and function.status == "partial"
        assert function.result == {"symbols": ["AAPL"], "failed_symbols": ["MSFT"]}


async def test_final_failed_retry_preserves_previous_partial_result(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    engine, sessions = orchestration_database
    now = datetime.now(UTC)
    async with sessions.begin() as database:
        job = JobRun(
            job_key="test_partial_retry",
            kind="function",
            trigger="manual",
            registry_version="test",
            registry_snapshot={},
            edition_date=now.date(),
            deadline_at=now + timedelta(hours=1),
            status="pending",
        )
        database.add(job)
        await database.flush()
        database.add(
            FunctionRun(
                job_run_id=job.id,
                function_key="commodity_daily_bars",
                provider_key="twelve_data",
                scope={},
                status="pending",
            )
        )

    first = await claim_ready_function(engine, sessions, owner="partial-retry", now=now)
    assert first is not None

    async def partial(_: ClaimedFunction) -> FunctionOutcome:
        return FunctionOutcome(
            status="partial",
            result={"symbols": ["WTI"], "failed_symbols": ["BRENT", "COPPER"]},
            missing_scopes=("BRENT", "COPPER"),
            retryable=True,
        )

    await execute_claimed(first, sessions, partial)
    async with sessions() as database:
        function = await database.get(FunctionRun, first.function_run_id)
        assert function is not None and function.next_attempt_at is not None
        retry_at = function.next_attempt_at

    second = await claim_ready_function(engine, sessions, owner="partial-retry", now=retry_at)
    assert second is not None
    assert second.function_run_id == first.function_run_id

    async def second_partial(_: ClaimedFunction) -> FunctionOutcome:
        return FunctionOutcome(
            status="partial",
            result={"symbols": ["BRENT"], "failed_symbols": ["COPPER"]},
            missing_scopes=("COPPER",),
            retryable=True,
        )

    await execute_claimed(second, sessions, second_partial)
    async with sessions() as database:
        function = await database.get(FunctionRun, second.function_run_id)
        assert function is not None and function.next_attempt_at is not None
        final_retry_at = function.next_attempt_at

    third = await claim_ready_function(engine, sessions, owner="partial-retry", now=final_retry_at)
    assert third is not None
    assert third.function_run_id == first.function_run_id
    async with sessions.begin() as database:
        claimed_job = await database.get(JobRun, third.job_run_id)
        assert claimed_job is not None
        claimed_job.deadline_at = datetime.now(UTC) + timedelta(minutes=1)

    async def failed(_: ClaimedFunction) -> FunctionOutcome:
        return FunctionOutcome(
            status="failed",
            result={"failed_symbols": ["COPPER"], "reason": "upstream_error"},
            missing_scopes=("COPPER",),
            error_code="upstream_error",
            retryable=True,
        )

    await execute_claimed(third, sessions, failed)
    async with sessions() as database:
        function = await database.get(FunctionRun, third.function_run_id)
        assert function is not None and function.status == "partial"
        assert function.result == {
            "symbols": ["WTI", "BRENT"],
            "failed_symbols": ["COPPER"],
        }
        attempts = list(
            await database.scalars(
                select(FunctionAttempt)
                .where(FunctionAttempt.function_run_id == function.id)
                .order_by(FunctionAttempt.attempt_number)
            )
        )
        assert [attempt.status for attempt in attempts] == ["partial", "partial", "failed"]
        assert attempts[-1].result == {
            "failed_symbols": ["COPPER"],
            "reason": "upstream_error",
        }


@pytest.mark.parametrize("terminal_status", ["succeeded", "no_change"])
async def test_terminal_retry_merges_successes_from_previous_partial_attempts(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    terminal_status: str,
) -> None:
    engine, sessions = orchestration_database
    now = datetime.now(UTC)
    async with sessions.begin() as database:
        job = JobRun(
            job_key="test_partial_then_success",
            kind="function",
            trigger="manual",
            registry_version="test",
            registry_snapshot={},
            edition_date=now.date(),
            deadline_at=now + timedelta(hours=1),
            status="pending",
        )
        database.add(job)
        await database.flush()
        database.add(
            FunctionRun(
                job_run_id=job.id,
                function_key="commodity_daily_bars",
                provider_key="twelve_data",
                scope={},
                status="pending",
            )
        )

    first = await claim_ready_function(engine, sessions, owner="partial-success", now=now)
    assert first is not None

    async def partial(_: ClaimedFunction) -> FunctionOutcome:
        return FunctionOutcome(
            status="partial",
            result={"symbols": ["WTI"], "failed_symbols": ["BRENT"]},
            missing_scopes=("BRENT",),
            retryable=True,
        )

    await execute_claimed(first, sessions, partial)
    async with sessions() as database:
        function = await database.get(FunctionRun, first.function_run_id)
        assert function is not None and function.next_attempt_at is not None
        retry_at = function.next_attempt_at

    second = await claim_ready_function(engine, sessions, owner="partial-success", now=retry_at)
    assert second is not None

    async def recovered(_: ClaimedFunction) -> FunctionOutcome:
        return FunctionOutcome(
            status=cast(AttemptStatus, terminal_status),
            result={"symbols": ["BRENT"], "failed_symbols": []},
        )

    await execute_claimed(second, sessions, recovered)
    async with sessions() as database:
        function = await database.get(FunctionRun, second.function_run_id)
        loaded_job = await database.get(JobRun, second.job_run_id)
        assert function is not None and function.status == terminal_status
        assert function.result == {"symbols": ["WTI", "BRENT"], "failed_symbols": []}
        assert loaded_job is not None and loaded_job.status == "succeeded"
        assert loaded_job.result == function.result


async def test_deadline_recovers_expired_running_lease_and_reconciles_job(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = orchestration_database
    now = datetime.now(UTC)
    fence_token = uuid.uuid4()
    async with sessions.begin() as database:
        job = JobRun(
            job_key="test_expired_running_lease",
            kind="function",
            trigger="automatic",
            automatic_key="test-expired-running-lease",
            registry_version="test",
            registry_snapshot={},
            edition_date=now.date(),
            deadline_at=now - timedelta(minutes=1),
            status="running",
        )
        database.add(job)
        await database.flush()
        function = FunctionRun(
            job_run_id=job.id,
            function_key="commodity_daily_bars",
            provider_key="twelve_data",
            scope={},
            status="running",
            attempt_count=1,
            lease_owner="expired-worker",
            lease_token=fence_token,
            lease_expires_at=now - timedelta(seconds=1),
        )
        database.add(function)
        await database.flush()
        attempt = FunctionAttempt(
            function_run_id=function.id,
            attempt_number=1,
            provider_key="twelve_data",
            function_key="commodity_daily_bars",
            scope={},
            status="running",
            fence_token=fence_token,
            started_at=now - timedelta(minutes=5),
        )
        database.add(attempt)
        await database.flush()
        function_id = function.id
        attempt_id = attempt.id
        job_id = job.id

    async with sessions() as database:
        assert await terminalize_expired_automatic_functions(database, now=now) == 1
    await reconcile_function_jobs(sessions, now=now)

    async with sessions() as database:
        loaded_function = await database.get(FunctionRun, function_id)
        loaded_attempt = await database.get(FunctionAttempt, attempt_id)
        loaded_job = await database.get(JobRun, job_id)
        assert loaded_function is not None and loaded_function.status == "unavailable"
        assert loaded_function.lease_owner is None
        assert loaded_function.lease_token is None
        assert loaded_function.lease_expires_at is None
        assert loaded_attempt is not None and loaded_attempt.status == "failed"
        assert loaded_attempt.error_code == "lease_expired"
        assert loaded_job is not None and loaded_job.status == "failed"


async def test_deadline_terminalization_processes_a_bounded_batch(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = orchestration_database
    now = datetime.now(UTC)
    async with sessions.begin() as database:
        job = JobRun(
            job_key="deadline_batch_test",
            kind="function",
            trigger="automatic",
            automatic_key="deadline-batch-test",
            registry_version="test",
            registry_snapshot={},
            edition_date=now.date(),
            deadline_at=now - timedelta(minutes=1),
            status="running",
        )
        database.add(job)
        await database.flush()
        for index in range(RECONCILIATION_BATCH_SIZE + 1):
            function = FunctionRun(
                job_run_id=job.id,
                function_key=f"deadline_test_{index}",
                provider_key="twelve_data",
                scope={},
                status="pending",
                created_at=now + timedelta(microseconds=index),
            )
            database.add(function)
            await database.flush()
            database.add(
                FunctionAttempt(
                    function_run_id=function.id,
                    attempt_number=1,
                    provider_key="twelve_data",
                    function_key=function.function_key,
                    scope={},
                    fence_token=uuid.uuid4(),
                    status="partial",
                    finished_at=now - timedelta(minutes=2),
                    request_metadata=[],
                )
            )

    async with sessions() as database:
        assert (
            await terminalize_expired_automatic_functions(database, now=now)
            == RECONCILIATION_BATCH_SIZE
        )
    async with sessions() as database:
        status_counts: dict[str, int] = {
            status: count
            for status, count in (
                await database.execute(
                    select(FunctionRun.status, func.count()).group_by(FunctionRun.status)
                )
            ).all()
        }
        assert status_counts == {"partial": RECONCILIATION_BATCH_SIZE, "pending": 1}

    async with sessions() as database:
        assert await terminalize_expired_automatic_functions(database, now=now) == 1
    async with sessions() as database:
        status_counts = {
            status: count
            for status, count in (
                await database.execute(
                    select(FunctionRun.status, func.count()).group_by(FunctionRun.status)
                )
            ).all()
        }
        assert status_counts == {"partial": RECONCILIATION_BATCH_SIZE + 1}


async def test_reconcile_function_jobs_processes_a_bounded_eligible_batch(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = orchestration_database
    now = datetime.now(UTC)
    async with sessions.begin() as database:
        for index in range(RECONCILIATION_BATCH_SIZE + 1):
            job = JobRun(
                job_key=f"reconcile_test_{index}",
                kind="function",
                trigger="manual",
                registry_version="test",
                registry_snapshot={},
                edition_date=now.date(),
                status="running",
                created_at=now + timedelta(microseconds=index),
            )
            database.add(job)
            await database.flush()
            database.add(
                FunctionRun(
                    job_run_id=job.id,
                    function_key="commodity_daily_bars",
                    provider_key="twelve_data",
                    scope={},
                    status="succeeded",
                    completed_at=now,
                )
            )

    await reconcile_function_jobs(sessions, now=now)
    async with sessions() as database:
        active_after_first_pass = await database.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(JobRun.status.in_(("pending", "running")))
        )
        assert active_after_first_pass == 1

    await reconcile_function_jobs(sessions, now=now)
    async with sessions() as database:
        active_after_second_pass = await database.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(JobRun.status.in_(("pending", "running")))
        )
        assert active_after_second_pass == 0


async def test_aggregate_routines_processes_a_bounded_eligible_batch(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = orchestration_database
    now = datetime.now(UTC)
    async with sessions.begin() as database:
        for index in range(RECONCILIATION_BATCH_SIZE + 1):
            routine = RoutineRun(
                routine_key=f"reconcile_test_{index}",
                registry_version="test",
                registry_digest="0" * 64,
                registry_snapshot={},
                edition_date=now.date(),
                scheduled_for=now,
                deadline_at=now + timedelta(hours=2),
                status="running",
                created_at=now + timedelta(microseconds=index),
            )
            database.add(routine)
            await database.flush()
            database.add(
                JobRun(
                    routine_run_id=routine.id,
                    job_key="twelve_data_daily_update",
                    kind="function",
                    trigger="automatic",
                    automatic_key=f"reconcile-test-{index}",
                    registry_version="test",
                    registry_snapshot={},
                    edition_date=now.date(),
                    status="succeeded",
                    completed_at=now,
                )
            )

    await aggregate_routines(sessions, now=now)
    async with sessions() as database:
        active_after_first_pass = await database.scalar(
            select(func.count())
            .select_from(RoutineRun)
            .where(RoutineRun.status.in_(("pending", "running")))
        )
        assert active_after_first_pass == 1

    await aggregate_routines(sessions, now=now)
    async with sessions() as database:
        active_after_second_pass = await database.scalar(
            select(func.count())
            .select_from(RoutineRun)
            .where(RoutineRun.status.in_(("pending", "running")))
        )
        assert active_after_second_pass == 0


async def test_analyst_failure_persists_failed_sync_run(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sessions = orchestration_database
    attempt_id = uuid.uuid4()
    fence_token = uuid.uuid4()
    edition = date(2026, 9, 17)
    async with sessions.begin() as database:
        job = JobRun(
            job_key="analyst_viewpoints_refresh",
            kind="function",
            trigger="manual",
            registry_version="test",
            registry_snapshot={},
            edition_date=edition,
            status="running",
        )
        database.add(job)
        await database.flush()
        function = FunctionRun(
            job_run_id=job.id,
            function_key="analyst_viewpoints_sync",
            provider_key="internal_services",
            scope={},
            status="running",
            lease_token=fence_token,
        )
        database.add(function)
        await database.flush()
        database.add(
            FunctionAttempt(
                id=attempt_id,
                function_run_id=function.id,
                attempt_number=1,
                provider_key="internal_services",
                function_key="analyst_viewpoints_sync",
                scope={},
                fence_token=fence_token,
                status="running",
                request_metadata=[],
            )
        )

    async def fail_sync(*_: object, **__: object) -> object:
        raise AnalystViewpointSyncError("provider unavailable", code="upstream_unavailable")

    monkeypatch.setattr(orchestration_functions, "sync_viewpoints", fail_sync)
    claimed = ClaimedFunction(
        connection=cast(AsyncConnection, None),
        function_run_id=function.id,
        job_run_id=job.id,
        attempt_id=attempt_id,
        function_key="analyst_viewpoints_sync",
        provider_key="internal_services",
        edition_date=edition,
        fence_token=fence_token,
        deadline_at=None,
        scope={},
    )
    settings = Settings(
        environment="test",
        analyst_viewpoints_enabled=True,
        analyst_viewpoints_base_url="https://analyst.example.test",
        analyst_viewpoints_api_key="test-analyst-key",
    )

    with pytest.raises(AnalystViewpointSyncError, match="provider unavailable"):
        await orchestration_functions._run_analyst(settings, sessions, claimed)

    async with sessions() as database:
        run = await database.scalar(select(AnalystViewpointSyncRun))
        assert run is not None
        assert run.viewpoint_date == edition
        assert run.trigger == "manual"
        assert run.status == "failed"
        assert run.error_code == "upstream_unavailable"
        assert run.function_attempt_id == attempt_id


async def test_ready_candidates_selects_oldest_run_per_provider_before_limit(
    orchestration_database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, sessions = orchestration_database
    now = datetime.now(UTC)
    async with sessions.begin() as database:
        for index in range(101):
            job = JobRun(
                job_key="twelve_data_daily_update",
                kind="function",
                trigger="manual",
                registry_version="test",
                registry_snapshot={},
                edition_date=now.date(),
                deadline_at=now + timedelta(hours=1),
                status="pending",
                created_at=now - timedelta(minutes=200 - index),
            )
            database.add(job)
            await database.flush()
            database.add(
                FunctionRun(
                    job_run_id=job.id,
                    function_key="commodity_daily_bars",
                    provider_key="twelve_data",
                    scope={},
                    status="pending",
                    created_at=job.created_at,
                )
            )
        yahoo_job = JobRun(
            job_key="yahoo_finance_daily_update",
            kind="function",
            trigger="manual",
            registry_version="test",
            registry_snapshot={},
            edition_date=now.date(),
            deadline_at=now + timedelta(hours=1),
            status="pending",
            created_at=now,
        )
        database.add(yahoo_job)
        await database.flush()
        yahoo_function = FunctionRun(
            job_run_id=yahoo_job.id,
            function_key="us_index_daily_bars",
            provider_key="yahoo_finance",
            scope={},
            status="pending",
            created_at=now,
        )
        database.add(yahoo_function)
        await database.flush()
        yahoo_function_id = yahoo_function.id

    candidates = await _ready_candidates(sessions, now)

    assert len(candidates) == 2
    assert (yahoo_function_id, "yahoo_finance") in candidates
