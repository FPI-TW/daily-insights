import asyncio
import subprocess
import sys
import uuid
from collections.abc import Awaitable
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

import daily_insights_api.scripts.run_orchestration_worker as orchestration_worker_script
from daily_insights_api.core.config import Settings
from daily_insights_api.modules.news.contracts import SelectedCandidate, Selection
from daily_insights_api.modules.news.llm import ModelCall
from daily_insights_api.modules.news.models import NewsCandidateBatch, PreparedNewsItem
from daily_insights_api.modules.orchestration import worker
from daily_insights_api.modules.orchestration.models import JobRun
from daily_insights_api.modules.orchestration.news_functions import (
    _publication_digest,
    _refresh_status,
    _restore_model_call,
    _serialize_model_call,
)
from daily_insights_api.modules.orchestration.projections import (
    FrozenObservation,
    _macro_history_identity,
    _macro_history_specs,
    _projection_datasets,
    _report_bundle,
    _rows_digest,
    _rows_for_current_outcomes,
)
from daily_insights_api.modules.orchestration.registry import (
    DAILY_ROUTINE,
    FUNCTION_BY_KEY,
    JOB_BY_KEY,
    PROVIDER_BY_KEY,
    registry_digest,
    validate_registry,
)
from daily_insights_api.modules.orchestration.service import next_retry_at, routine_window
from daily_insights_api.modules.orchestration.worker import (
    ClaimedFunction,
    FunctionOutcome,
    execute_claimed,
    provider_lock_key,
)
from daily_insights_api.modules.reports.launch_manifest import ACTIVE_LAUNCH_MANIFEST
from daily_insights_api.modules.reports.morning_report import MORNING_REPORT_DERIVATION_VERSION
from daily_insights_api.scripts.run_orchestration_dispatcher import DispatcherSettings


def test_registry_has_expected_provider_function_job_relationships() -> None:
    validate_registry()

    assert FUNCTION_BY_KEY["commodity_daily_bars"].provider_key == "twelve_data"
    assert FUNCTION_BY_KEY["us_index_daily_bars"].provider_key == "yahoo_finance"
    assert FUNCTION_BY_KEY["treasury_yield_curve"].provider_key == "us_treasury"
    assert FUNCTION_BY_KEY["sofr_daily_rates"].provider_key == "new_york_fed"
    assert FUNCTION_BY_KEY["news_publish"].provider_key == "internal_services"
    assert set(PROVIDER_BY_KEY) == {
        "twelve_data",
        "yahoo_finance",
        "twse",
        "us_treasury",
        "new_york_fed",
        "internal_services",
    }
    assert len(registry_digest()) == 64


def test_dispatcher_production_settings_require_only_database_orchestration_scope() -> None:
    settings = DispatcherSettings(
        environment="production",
        database_url="postgresql+psycopg://example.invalid/daily_insights",
        orchestration_enabled=True,
        orchestration_activation_date=date(2026, 9, 17),
    )

    assert settings.environment == "production"
    assert settings.orchestration_enabled


def test_dispatcher_registers_foreign_key_targets_in_an_isolated_process() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import daily_insights_api.scripts.run_orchestration_dispatcher; "
                "from daily_insights_api.modules.orchestration.models import JobRun; "
                "assert next(iter(JobRun.__table__.c.requested_by_user_id.foreign_keys))."
                "column.table.name == 'users'"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_news_daily_job_refreshes_all_markets_before_publish() -> None:
    news = JOB_BY_KEY["news_daily_update"]
    publish = next(step for step in news.functions if step.function_key == "news_publish")

    assert news.triggers == ("manual",)
    assert publish.depends_on == (
        "news_global_refresh",
        "news_tw_equity_refresh",
        "news_us_equity_refresh",
    )
    assert publish.dependency_policy == "terminal"


def test_news_generation_failures_and_editorial_shortfalls_are_terminal() -> None:
    assert _refresh_status(2, 1) == ("partial", "partial", False)
    assert _refresh_status(2, 0) == ("succeeded", "ready", False)
    assert _refresh_status(0, 1) == ("unavailable", "unavailable", False)
    assert _refresh_status(0, 0) == ("unavailable", "unavailable", False)


def test_selection_checkpoint_preserves_returned_and_rejected_candidates() -> None:
    kept = SelectedCandidate(
        id="a" * 64,
        topic="markets",
        event_key="kept-event",
        market="global",
        importance=5,
    )
    rejected = kept.model_copy(
        update={"id": "b" * 64, "event_key": "rejected-event", "market": "asia"}
    )
    original = ModelCall(
        Selection(selections=(kept,)),
        "request-id",
        10,
        5,
        1,
        "c" * 64,
        rejected=((rejected, "off_market"),),
        returned=(kept, rejected),
    )

    restored = _restore_model_call(_serialize_model_call(original), stage="selection")

    assert restored.reused is True
    assert restored.returned == original.returned
    assert restored.rejected == original.rejected

    batch = NewsCandidateBatch(
        function_attempt_id=uuid.uuid4(),
        edition_date=date(2026, 9, 17),
        market_code="global",
        status="partial",
        input_digest="a" * 64,
    )

    def prepared(rank: int) -> PreparedNewsItem:
        return PreparedNewsItem(
            batch_id=uuid.uuid4(),
            candidate_id=uuid.uuid4(),
            rank=rank,
            topic="markets",
            importance=4,
            market="global",
            event_key=f"event-{rank}",
            numeric_facts=[],
            presentations={"en": {"headline": f"Story {rank}", "summary": "Summary"}},
            content_digest=str(rank) * 64,
        )

    first = prepared(1)
    second = prepared(2)
    partial_digest = _publication_digest(batch, [first])
    complete_digest = _publication_digest(batch, [first, second])
    assert partial_digest != complete_digest
    assert complete_digest == _publication_digest(batch, [first, second])


def test_automatic_provider_jobs_are_unique_and_internal_services_are_combined() -> None:
    automatic_provider_jobs = [
        job for job in JOB_BY_KEY.values() if job.kind == "function" and "automatic" in job.triggers
    ]
    assert [job.automatic_key for job in automatic_provider_jobs] == [
        "twelve_data",
        "yahoo_finance",
        "twse",
        "us_treasury",
        "new_york_fed",
        "internal_services",
    ]
    internal = JOB_BY_KEY["internal_services_daily_update"]
    assert {step.function_key for step in internal.functions} == {
        "news_global_refresh",
        "news_tw_equity_refresh",
        "news_us_equity_refresh",
        "news_publish",
        "analyst_viewpoints_sync",
    }


def test_manual_news_market_jobs_publish_after_their_refresh() -> None:
    for job_key, function_key in (
        ("news_global_refresh_job", "news_global_refresh"),
        ("news_tw_equity_refresh_job", "news_tw_equity_refresh"),
        ("news_us_equity_refresh_job", "news_us_equity_refresh"),
    ):
        job = JOB_BY_KEY[job_key]
        publish = next(step for step in job.functions if step.function_key == "news_publish")
        assert publish.depends_on == (function_key,)
        assert publish.dependency_policy == "terminal"


def test_projection_jobs_are_not_provider_functions() -> None:
    for key in ("market_reports_publish", "macro_dashboard_publish"):
        job = JOB_BY_KEY[key]
        assert job.kind == "projection"
        assert job.functions == ()
        assert job.projection_handler == key


def test_projection_freezes_only_required_datasets() -> None:
    us_job = JobRun(
        job_key="market_reports_publish",
        kind="projection",
        trigger="manual",
        registry_version="test",
        registry_snapshot={},
        edition_date=date(2026, 9, 17),
        payload={"requested_market_job": "us_equity_refresh"},
    )
    macro_job = JobRun(
        job_key="macro_dashboard_publish",
        kind="projection",
        trigger="manual",
        registry_version="test",
        registry_snapshot={},
        edition_date=date(2026, 9, 17),
    )

    assert _projection_datasets(us_job) == frozenset({"us_mega_cap_daily_bars"})
    assert "us_mega_cap_daily_bars" not in _projection_datasets(macro_job)
    assert "treasury_yield_curve" in _projection_datasets(macro_job)


def test_routine_window_and_retry_respect_taipei_soft_deadline() -> None:
    scheduled, deadline = routine_window(date(2026, 9, 17))

    assert scheduled == datetime(2026, 9, 17, 8, tzinfo=ZoneInfo("Asia/Taipei"))
    assert deadline == datetime(2026, 9, 17, 10, tzinfo=ZoneInfo("Asia/Taipei"))
    assert next_retry_at(scheduled, deadline) == datetime(
        2026, 9, 17, 8, 30, tzinfo=ZoneInfo("Asia/Taipei")
    )
    assert (
        next_retry_at(datetime(2026, 9, 17, 9, 45, tzinfo=ZoneInfo("Asia/Taipei")), deadline)
        is None
    )
    assert DAILY_ROUTINE.deadline_hour == 10


def test_provider_locks_are_stable_and_separate() -> None:
    assert provider_lock_key("twse") == provider_lock_key("twse")
    assert provider_lock_key("twse") != provider_lock_key("twelve_data")


def _observation(
    dataset: str,
    symbol: str,
    observed: date,
    value: int,
    *,
    unit: str = "USD",
) -> FrozenObservation:
    return FrozenObservation(
        kind="market",
        id=uuid.uuid4(),
        function_attempt_id=uuid.uuid4(),
        provider_key="twelve_data",
        dataset_key=dataset,
        symbol=symbol,
        unit=unit,
        observation_date=observed,
        value=Decimal(value),
        open_value=Decimal(value - 1),
        value_digest=f"{dataset}:{symbol}:{observed}",
    )


def test_projection_report_bundle_preserves_existing_publication_contract() -> None:
    end = date(2026, 9, 15)
    rows: list[FrozenObservation] = []
    for offset in range(740):
        observed = end - timedelta(days=739 - offset)
        rows.extend(
            _observation("commodity_daily_bars", symbol, observed, 100 + offset)
            for symbol in ("XBR/USD", "WTI/USD", "XAU/USD", "XAG/USD", "HG1")
        )
    for offset in range(2):
        observed = end - timedelta(days=1 - offset)
        rows.extend(
            _observation("rates_proxy_daily_bars", symbol, observed, 100 + offset)
            for symbol in ("TLT", "IEF", "UUP")
        )
        rows.extend(
            _observation("fx_daily_bars", symbol, observed, 100 + offset)
            for symbol in ("USD/TWD", "USD/JPY", "EUR/USD")
        )

    bundle = _report_bundle("global_macro_bonds", tuple(rows))

    assert bundle.content.status == "complete"
    assert tuple(block.id for block in bundle.content.blocks) == (
        "macro.commodities",
        "macro.rates_fx",
        "macro.commodity_ratios",
    )
    assert bundle.content.schema_version == "three-market.v1"
    assert set(bundle.presentations) == {"zh-hant", "zh-hans", "en"}
    assert ACTIVE_LAUNCH_MANIFEST.version
    assert ACTIVE_LAUNCH_MANIFEST.sha256
    assert MORNING_REPORT_DERIVATION_VERSION == "twelve-data.three-market.v9"


def test_macro_projection_uses_dashboard_canonical_history_ids_and_units() -> None:
    assert _macro_history_identity("commodity_daily_bars", "XAU/USD", "USD") == (
        "gold",
        "USD/oz",
    )
    assert _macro_history_identity("treasury_yield_curve", "BC_10YEAR", "percent") == (
        "10y",
        "percent",
    )
    assert _macro_history_identity("sofr_daily_rates", "SOFR", "percent") == (
        "sofr",
        "percent",
    )
    assert _macro_history_identity("dxy_daily_bars", "DX-Y.NYB", "index") == (
        "dxy",
        "index",
    )
    assert _macro_history_identity("fx_daily_bars", "USD/TWD", "TWD") == (
        "usd_twd",
        "TWD",
    )
    specs = _macro_history_specs()
    assert len(specs) == 26
    assert len({identifier for _, identifier, _, _, _ in specs}) == 26
    assert all(dataset != "rates_proxy_daily_bars" for dataset, *_ in specs)


def test_report_projection_digest_includes_provider_and_observation_date() -> None:
    first = _observation("us_mega_cap_daily_bars", "AAPL", date(2026, 9, 14), 100)
    moved = replace(first, observation_date=date(2026, 9, 15))
    moved_provider = replace(first, provider_key="corrected_provider")

    assert _rows_digest("us_equity", (first,)) != _rows_digest("us_equity", (moved,))
    assert _rows_digest("us_equity", (first,)) != _rows_digest("us_equity", (moved_provider,))
    succeeded = (
        {
            "function_key": "us_mega_cap_daily_bars",
            "provider_key": "twelve_data",
            "status": "succeeded",
            "error": None,
            "missing_scopes": [],
            "attempt_ids": [str(uuid.uuid4())],
        },
    )
    same_result_new_attempt = ({**succeeded[0], "attempt_ids": [str(uuid.uuid4())]},)
    no_change = ({**same_result_new_attempt[0], "status": "no_change"},)
    failed = ({**succeeded[0], "status": "failed", "error": "upstream"},)
    assert _rows_digest("us_equity", (first,), succeeded) == _rows_digest(
        "us_equity", (first,), same_result_new_attempt
    )
    assert _rows_digest("us_equity", (first,), succeeded) == _rows_digest(
        "us_equity", (first,), no_change
    )
    assert _rows_digest("us_equity", (first,), succeeded) != _rows_digest(
        "us_equity", (first,), failed
    )


def test_partial_outcome_keeps_unchanged_successful_scopes() -> None:
    aapl = _observation("us_mega_cap_daily_bars", "AAPL", date(2026, 9, 15), 100)
    msft = _observation("us_mega_cap_daily_bars", "MSFT", date(2026, 9, 15), 200)
    outcomes = (
        {
            "function_key": "us_mega_cap_daily_bars",
            "provider_key": "twelve_data",
            "status": "partial",
            "error": "partial_symbols",
            "missing_scopes": ["MSFT"],
            "successful_scopes": ["AAPL"],
            "attempt_ids": [str(uuid.uuid4())],
        },
    )

    assert _rows_for_current_outcomes((aapl, msft), outcomes) == (aapl,)


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def test_heartbeat_error_still_releases_provider_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    claimed = ClaimedFunction(
        connection=cast(AsyncConnection, connection),
        function_run_id=uuid.uuid4(),
        job_run_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        function_key="commodity_daily_bars",
        provider_key="twelve_data",
        edition_date=date(2026, 9, 17),
        fence_token=uuid.uuid4(),
        deadline_at=None,
        scope={},
    )
    unlocked: list[str] = []

    async def immediate_timeout(awaitable: Awaitable[Any], **kwargs: object) -> None:
        del kwargs
        cast(Any, awaitable).close()
        raise TimeoutError

    async def heartbeat_error(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        raise RuntimeError("heartbeat unavailable")

    async def finish(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    async def unlock(_connection: AsyncConnection, provider_key: str) -> None:
        unlocked.append(provider_key)

    async def handler(_: ClaimedFunction) -> FunctionOutcome:
        await asyncio.sleep(0)
        return FunctionOutcome(status="succeeded")

    monkeypatch.setattr(asyncio, "wait_for", immediate_timeout)
    monkeypatch.setattr(worker, "heartbeat_function", heartbeat_error)
    monkeypatch.setattr(worker, "finish_function", finish)
    monkeypatch.setattr(worker, "_unlock_provider", unlock)

    with pytest.raises(RuntimeError, match="heartbeat unavailable"):
        await execute_claimed(
            claimed,
            cast(async_sessionmaker[AsyncSession], object()),
            handler,
        )

    assert unlocked == ["twelve_data"]
    assert connection.closed


async def test_unexpected_news_publish_exception_is_safe_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    claimed = ClaimedFunction(
        connection=cast(AsyncConnection, connection),
        function_run_id=uuid.uuid4(),
        job_run_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        function_key="news_publish",
        provider_key="internal_services",
        edition_date=date(2026, 9, 17),
        fence_token=uuid.uuid4(),
        deadline_at=datetime.now(ZoneInfo("UTC")) + timedelta(hours=1),
        scope={},
    )
    outcomes: list[FunctionOutcome] = []

    async def failing(_: ClaimedFunction) -> FunctionOutcome:
        raise RuntimeError("unexpected publish error")

    async def finish(
        _sessions: async_sessionmaker[AsyncSession],
        _claimed: ClaimedFunction,
        outcome: FunctionOutcome,
    ) -> bool:
        outcomes.append(outcome)
        return True

    async def unlock(_: AsyncConnection, __: str) -> None:
        return None

    monkeypatch.setattr(worker, "finish_function", finish)
    monkeypatch.setattr(worker, "_unlock_provider", unlock)

    await execute_claimed(
        claimed,
        cast(async_sessionmaker[AsyncSession], object()),
        failing,
    )

    assert len(outcomes) == 1
    assert outcomes[0].status == "failed"
    assert outcomes[0].retryable is False
    assert outcomes[0].error_code == "unexpected_error"
    assert outcomes[0].error_detail == "unexpected_error"
    assert connection.closed


async def test_disabled_worker_remains_alive_and_refreshes_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopIdleLoop(Exception):
        pass

    class Heartbeat:
        def __init__(self) -> None:
            self.touches = 0

        async def touch(self) -> None:
            self.touches += 1

    heartbeat = Heartbeat()

    async def stop_after_first_interval(seconds: float) -> None:
        assert seconds == 1
        raise StopIdleLoop

    monkeypatch.setattr(
        orchestration_worker_script,
        "get_settings",
        lambda: Settings(
            environment="test",
            orchestration_enabled=False,
            orchestration_poll_seconds=1,
        ),
    )
    monkeypatch.setattr(orchestration_worker_script, "HEARTBEAT_PATH", heartbeat)
    monkeypatch.setattr(asyncio, "sleep", stop_after_first_interval)

    with pytest.raises(StopIdleLoop):
        await orchestration_worker_script.worker_loop()

    assert heartbeat.touches == 1


async def test_worker_claims_ready_projection_before_function_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Heartbeat:
        async def touch(self) -> None:
            return None

    class Engine:
        async def dispose(self) -> None:
            return None

    class Handlers(dict[str, object]):
        async def close(self) -> None:
            return None

    projection_available = True
    function_available = True

    async def claim_projection(*_: object, **__: object) -> object | None:
        nonlocal projection_available
        if not projection_available:
            return None
        projection_available = False
        calls.append("projection")
        return SimpleNamespace(job_run_id=uuid.uuid4(), fence_token=uuid.uuid4())

    async def claim_function(*_: object, **__: object) -> object | None:
        nonlocal function_available
        if not function_available:
            return None
        function_available = False
        calls.append("function")
        return SimpleNamespace(function_key="commodity_daily_bars")

    async def execute(*_: object, **__: object) -> None:
        return None

    async def reconcile(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        orchestration_worker_script,
        "get_settings",
        lambda: Settings(
            environment="test",
            orchestration_enabled=True,
            orchestration_worker_concurrency=2,
        ),
    )
    monkeypatch.setattr(orchestration_worker_script, "HEARTBEAT_PATH", Heartbeat())
    monkeypatch.setattr(orchestration_worker_script, "create_engine", lambda _: Engine())
    monkeypatch.setattr(orchestration_worker_script, "create_session_factory", lambda _: object())
    monkeypatch.setattr(
        orchestration_worker_script, "build_function_handlers", lambda *_: Handlers()
    )
    monkeypatch.setattr(orchestration_worker_script, "claim_ready_projection", claim_projection)
    monkeypatch.setattr(orchestration_worker_script, "claim_ready_function", claim_function)
    monkeypatch.setattr(orchestration_worker_script, "execute_projection", execute)
    monkeypatch.setattr(orchestration_worker_script, "execute_claimed", execute)
    monkeypatch.setattr(orchestration_worker_script, "reconcile_function_jobs", reconcile)

    await orchestration_worker_script.worker_loop(once=True)

    assert calls == ["projection", "function"]
