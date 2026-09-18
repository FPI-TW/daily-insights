from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.modules.data_sources.api import DailyBar, DataSourceContractError
from daily_insights_api.modules.orchestration.models import (
    FunctionAttempt,
    FunctionRun,
    InterestRateObservation,
    InterestRateSeries,
    JobDependency,
    JobRun,
    MarketDailyObservation,
    MarketDailySeries,
    ProjectionInputFreeze,
    ProjectionInputObservation,
    PublicationFunctionAttempt,
)
from daily_insights_api.modules.orchestration.registry import FUNCTION_BY_KEY, REGISTRY_VERSION
from daily_insights_api.modules.orchestration.service import (
    TERMINAL_RUN_STATUSES,
    next_retry_at,
)
from daily_insights_api.modules.reports.api import (
    ACTIVE_LAUNCH_MANIFEST,
    COMMODITIES,
    FX_INSTRUMENTS,
    INSTRUMENTS,
    MORNING_REPORT_DERIVATION_VERSION,
    TENORS,
    Calendar,
    ChartSeries,
    History,
    LaunchMarketCode,
    MacroDashboard,
    MacroDashboardSnapshot,
    MetricBlock,
    MetricItem,
    Point,
    PublicationBundle,
    ReportBlock,
    ReportPublication,
    SeriesBlock,
    TableBlock,
    TableCell,
    TableColumn,
    _bundle,
    _commodity_ratio_window_dates,
    _daily_change,
    _error_blocks_for_dataset,
    _normalized_points,
    _previous_close_change,
    _quantize,
    _ratio_common_date_points,
    block_precision,
    block_rounding,
)

PROJECTION_LEASE = timedelta(minutes=10)
MACRO_DASHBOARD_PUBLICATION_LOCK = int.from_bytes(
    hashlib.sha256(b"macro_dashboard:global_macro_bonds").digest()[:8],
    byteorder="big",
    signed=True,
)
REPORT_DATASETS: dict[str, tuple[str, ...]] = {
    "global_macro_bonds": (
        "commodity_daily_bars",
        "fx_daily_bars",
        "rates_proxy_daily_bars",
    ),
    "crypto": ("crypto_daily_bars",),
    "us_equity": ("us_mega_cap_daily_bars",),
}
MACRO_DATASETS = frozenset(
    (
        "commodity_daily_bars",
        "fx_daily_bars",
        "dxy_daily_bars",
        "treasury_yield_curve",
        "sofr_daily_rates",
    )
)
MACRO_MAX_HISTORY_AGE = timedelta(days=5)
MACRO_MIN_HISTORY_SPAN = timedelta(days=365)
MACRO_AVAILABLE_STATUSES = frozenset(("succeeded", "no_change"))


@dataclass(frozen=True, slots=True)
class FrozenObservation:
    kind: str
    id: uuid.UUID
    function_attempt_id: uuid.UUID
    provider_key: str
    dataset_key: str
    symbol: str
    unit: str
    observation_date: date
    value: Decimal
    open_value: Decimal | None
    value_digest: str
    is_provisional: bool = False


@dataclass(frozen=True, slots=True)
class ClaimedProjection:
    job_run_id: uuid.UUID
    fence_token: uuid.UUID


async def claim_ready_projection(
    session_factory: async_sessionmaker[AsyncSession], *, owner: str
) -> ClaimedProjection | None:
    now = datetime.now(UTC)
    async with session_factory.begin() as database:
        await database.execute(
            update(JobRun)
            .where(
                JobRun.kind == "projection",
                JobRun.deadline_at.is_not(None),
                JobRun.deadline_at <= now,
                or_(
                    and_(JobRun.status == "pending", JobRun.started_at.is_not(None)),
                    and_(
                        JobRun.status == "running",
                        JobRun.lease_expires_at.is_not(None),
                        JobRun.lease_expires_at < now,
                    ),
                ),
            )
            .values(
                status="failed",
                error="deadline_reached",
                next_attempt_at=None,
                completed_at=now,
                lease_owner=None,
                lease_token=None,
                lease_expires_at=None,
                heartbeat_at=now,
            )
        )
        candidates = (
            await database.scalars(
                select(JobRun)
                .where(
                    JobRun.kind == "projection",
                    JobRun.status.in_(("pending", "running")),
                    (JobRun.next_attempt_at.is_(None) | (JobRun.next_attempt_at <= now)),
                    (JobRun.lease_expires_at.is_(None) | (JobRun.lease_expires_at < now)),
                    or_(
                        JobRun.deadline_at.is_(None),
                        JobRun.deadline_at > now,
                        and_(JobRun.status == "pending", JobRun.started_at.is_(None)),
                    ),
                )
                .order_by(JobRun.created_at, JobRun.id)
                .with_for_update(skip_locked=True)
                .limit(20)
            )
        ).all()
        for job in candidates:
            if not await _dependencies_terminal(database, job.id):
                continue
            token = uuid.uuid4()
            job.status = "running"
            job.started_at = job.started_at or now
            job.lease_owner = owner
            job.lease_token = token
            job.lease_expires_at = now + PROJECTION_LEASE
            job.heartbeat_at = now
            return ClaimedProjection(job_run_id=job.id, fence_token=token)
    return None


async def _dependencies_terminal(database: AsyncSession, job_run_id: uuid.UUID) -> bool:
    statuses = (
        await database.scalars(
            select(JobRun.status)
            .join(JobDependency, JobDependency.upstream_job_run_id == JobRun.id)
            .where(JobDependency.downstream_job_run_id == job_run_id)
        )
    ).all()
    return all(status in TERMINAL_RUN_STATUSES for status in statuses)


async def execute_projection(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_run_id: uuid.UUID,
    owner: str,
    fence_token: uuid.UUID,
) -> None:
    heartbeat_stop = asyncio.Event()

    async def heartbeats() -> None:
        while not heartbeat_stop.is_set():
            try:
                await asyncio.wait_for(heartbeat_stop.wait(), timeout=30)
            except TimeoutError:
                if not await _heartbeat_projection(
                    session_factory,
                    job_run_id=job_run_id,
                    owner=owner,
                    fence_token=fence_token,
                ):
                    return

    heartbeat_task = asyncio.create_task(heartbeats())
    try:
        async with session_factory() as database:
            job = await database.get(JobRun, job_run_id)
            if (
                job is None
                or job.lease_owner != owner
                or job.lease_token != fence_token
                or job.status != "running"
            ):
                return
            job_key = job.job_key
        frozen = await freeze_projection_inputs(
            session_factory,
            job_run_id=job_run_id,
            owner=owner,
            fence_token=fence_token,
        )
        if job_key == "market_reports_publish":
            result = await publish_market_reports(
                session_factory,
                job_run_id=job_run_id,
                owner=owner,
                fence_token=fence_token,
                frozen=frozen,
            )
        elif job_key == "macro_dashboard_publish":
            result = await publish_macro_dashboard(
                session_factory,
                job_run_id=job_run_id,
                owner=owner,
                fence_token=fence_token,
                frozen=frozen,
            )
        else:
            raise ValueError(f"unknown projection job {job_key}")
        status = "partial" if result.get("partial") else "succeeded"
        error = None
    except Exception as caught:
        result = {}
        status = "failed"
        error = type(caught).__name__.lower()
    finally:
        heartbeat_stop.set()
        await heartbeat_task
    now = datetime.now(UTC)
    async with session_factory.begin() as database:
        job = await database.scalar(
            select(JobRun)
            .where(
                JobRun.id == job_run_id,
                JobRun.status == "running",
                JobRun.lease_owner == owner,
                JobRun.lease_token == fence_token,
            )
            .with_for_update()
        )
        if job is None:
            return
        retry_at = next_retry_at(now, job.deadline_at) if status == "failed" else None
        job.status = "pending" if retry_at is not None else status
        job.result = result
        job.error = error
        job.next_attempt_at = retry_at
        job.completed_at = None if retry_at is not None else now
        job.lease_owner = None
        job.lease_token = None
        job.lease_expires_at = None
        job.heartbeat_at = now


async def _heartbeat_projection(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_run_id: uuid.UUID,
    owner: str,
    fence_token: uuid.UUID,
) -> bool:
    now = datetime.now(UTC)
    async with session_factory.begin() as database:
        result = await database.execute(
            update(JobRun)
            .where(
                JobRun.id == job_run_id,
                JobRun.status == "running",
                JobRun.lease_owner == owner,
                JobRun.lease_token == fence_token,
            )
            .values(heartbeat_at=now, lease_expires_at=now + PROJECTION_LEASE)
        )
        return bool(cast(CursorResult[Any], result).rowcount)


async def freeze_projection_inputs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_run_id: uuid.UUID,
    owner: str,
    fence_token: uuid.UUID,
) -> tuple[FrozenObservation, ...]:
    async with session_factory.begin() as database:
        job = await database.scalar(
            select(JobRun)
            .where(
                JobRun.id == job_run_id,
                JobRun.status == "running",
                JobRun.lease_owner == owner,
                JobRun.lease_token == fence_token,
            )
            .with_for_update()
        )
        if job is None:
            raise ValueError("projection lease is no longer current")
        existing = await database.scalar(
            select(ProjectionInputFreeze).where(
                ProjectionInputFreeze.projection_job_run_id == job_run_id
            )
        )
        if existing is not None:
            return await _load_frozen(database, existing.id)
        cutoff = datetime.now(UTC)
        rows = await _latest_eligible_observations(
            database,
            job.edition_date,
            cutoff,
            datasets=_projection_datasets(job),
        )
        function_outcomes = await _current_projection_function_outcomes(database, job.id)
        payload = [
            {
                "kind": row.kind,
                "id": str(row.id),
                "provider": row.provider_key,
                "dataset": row.dataset_key,
                "symbol": row.symbol,
                "date": row.observation_date.isoformat(),
                "digest": row.value_digest,
            }
            for row in rows
        ]
        frozen_inputs = {
            "observations": payload,
            "function_outcomes": function_outcomes,
        }
        input_digest = hashlib.sha256(
            json.dumps(frozen_inputs, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        freeze = ProjectionInputFreeze(
            projection_job_run_id=job.id,
            registry_version=REGISTRY_VERSION,
            cutoff_at=cutoff,
            input_digest=input_digest,
            inputs=frozen_inputs,
        )
        database.add(freeze)
        await database.flush()
        database.add_all(
            ProjectionInputObservation(
                freeze_id=freeze.id, observation_kind=row.kind, observation_id=row.id
            )
            for row in rows
        )
        return rows


async def _current_projection_function_outcomes(
    database: AsyncSession, projection_job_run_id: uuid.UUID
) -> list[dict[str, Any]]:
    upstream_job_ids = select(JobDependency.upstream_job_run_id).where(
        JobDependency.downstream_job_run_id == projection_job_run_id
    )
    function_runs = list(
        (
            await database.scalars(
                select(FunctionRun)
                .where(FunctionRun.job_run_id.in_(upstream_job_ids))
                .order_by(FunctionRun.function_key, FunctionRun.id)
            )
        ).all()
    )
    if not function_runs:
        return []
    attempts = list(
        (
            await database.scalars(
                select(FunctionAttempt)
                .where(FunctionAttempt.function_run_id.in_(run.id for run in function_runs))
                .order_by(FunctionAttempt.function_run_id, FunctionAttempt.attempt_number)
            )
        ).all()
    )
    attempts_by_run: dict[uuid.UUID, list[FunctionAttempt]] = {}
    for attempt in attempts:
        attempts_by_run.setdefault(attempt.function_run_id, []).append(attempt)
    return [
        {
            "function_key": run.function_key,
            "provider_key": run.provider_key,
            "status": run.status,
            "error": run.error,
            "missing_scopes": run.missing_scopes or [],
            "successful_scopes": (
                run.result.get("symbols", [])
                if isinstance(run.result, dict) and isinstance(run.result.get("symbols"), list)
                else []
            ),
            "attempt_ids": [str(attempt.id) for attempt in attempts_by_run.get(run.id, [])],
        }
        for run in function_runs
    ]


async def _frozen_function_outcomes(
    database: AsyncSession, projection_job_run_id: uuid.UUID
) -> tuple[dict[str, Any], ...]:
    freeze = await database.scalar(
        select(ProjectionInputFreeze).where(
            ProjectionInputFreeze.projection_job_run_id == projection_job_run_id
        )
    )
    if freeze is None:
        raise ValueError("projection inputs have not been frozen")
    values = freeze.inputs.get("function_outcomes", [])
    return tuple(value for value in values if isinstance(value, dict))


def _rows_for_current_outcomes(
    rows: tuple[FrozenObservation, ...], outcomes: tuple[dict[str, Any], ...]
) -> tuple[FrozenObservation, ...]:
    by_function = {
        str(outcome.get("function_key")): outcome
        for outcome in outcomes
        if outcome.get("function_key")
    }
    partial_symbols: dict[str, set[str]] = {}
    for function_key, outcome in by_function.items():
        if outcome.get("status") != "partial":
            continue
        successful_scopes = {
            value for value in outcome.get("successful_scopes", []) if isinstance(value, str)
        }
        if successful_scopes:
            partial_symbols[function_key] = successful_scopes
            continue
        attempt_ids = {
            uuid.UUID(value) for value in outcome.get("attempt_ids", []) if isinstance(value, str)
        }
        partial_symbols[function_key] = {
            row.symbol
            for row in rows
            if row.dataset_key == function_key and row.function_attempt_id in attempt_ids
        }
    unavailable_statuses = {"unavailable", "failed", "cancelled"}
    return tuple(
        row
        for row in rows
        if by_function.get(row.dataset_key, {}).get("status") not in unavailable_statuses
        and (
            row.dataset_key not in partial_symbols or row.symbol in partial_symbols[row.dataset_key]
        )
    )


def _outcome_attempt_ids(outcomes: tuple[dict[str, Any], ...]) -> set[uuid.UUID]:
    return {
        uuid.UUID(value)
        for outcome in outcomes
        for value in outcome.get("attempt_ids", [])
        if isinstance(value, str)
    }


async def _latest_eligible_observations(
    database: AsyncSession,
    edition_date: date,
    cutoff: datetime,
    *,
    datasets: frozenset[str],
) -> tuple[FrozenObservation, ...]:
    history_start = edition_date - timedelta(days=740)
    market_rows = (
        await database.execute(
            select(MarketDailyObservation, MarketDailySeries)
            .join(MarketDailySeries, MarketDailySeries.id == MarketDailyObservation.series_id)
            .where(
                MarketDailyObservation.observation_date >= history_start,
                MarketDailyObservation.observation_date <= edition_date,
                MarketDailyObservation.created_at <= cutoff,
                MarketDailySeries.dataset_key.in_(datasets),
            )
            .distinct(
                MarketDailyObservation.series_id,
                MarketDailyObservation.observation_date,
            )
            .order_by(
                MarketDailyObservation.series_id,
                MarketDailyObservation.observation_date.desc(),
                MarketDailyObservation.version.desc(),
            )
        )
    ).all()
    rate_rows = (
        await database.execute(
            select(InterestRateObservation, InterestRateSeries)
            .join(InterestRateSeries, InterestRateSeries.id == InterestRateObservation.series_id)
            .where(
                InterestRateObservation.observation_date >= history_start,
                InterestRateObservation.observation_date <= edition_date,
                InterestRateObservation.created_at <= cutoff,
                InterestRateSeries.dataset_key.in_(datasets),
            )
            .distinct(
                InterestRateObservation.series_id,
                InterestRateObservation.observation_date,
            )
            .order_by(
                InterestRateObservation.series_id,
                InterestRateObservation.observation_date.desc(),
                InterestRateObservation.version.desc(),
            )
        )
    ).all()
    result: list[FrozenObservation] = []
    freshness: dict[tuple[str, uuid.UUID], bool] = {}
    for kind, pairs in (("market", market_rows), ("rate", rate_rows)):
        for observation, series in pairs:
            series_identity = (kind, series.id)
            definition = FUNCTION_BY_KEY.get(series.dataset_key)
            if definition is None:
                continue
            if series_identity not in freshness:
                freshness[series_identity] = (
                    edition_date - observation.observation_date
                ).days <= definition.freshness_days
            if not freshness[series_identity]:
                continue
            result.append(
                FrozenObservation(
                    kind=kind,
                    id=observation.id,
                    function_attempt_id=observation.function_attempt_id,
                    provider_key=series.provider_key,
                    dataset_key=series.dataset_key,
                    symbol=series.symbol,
                    unit=series.unit,
                    observation_date=observation.observation_date,
                    value=observation.close if kind == "market" else observation.value,
                    open_value=observation.open if kind == "market" else None,
                    is_provisional=(observation.is_provisional if kind == "market" else False),
                    value_digest=observation.value_digest,
                )
            )
    return tuple(result)


def _projection_datasets(job: JobRun) -> frozenset[str]:
    if job.job_key == "macro_dashboard_publish":
        return MACRO_DATASETS
    requested = (job.payload or {}).get("requested_market_job")
    if requested == "global_macro_refresh":
        return frozenset(REPORT_DATASETS["global_macro_bonds"])
    if requested == "us_equity_refresh":
        return frozenset(REPORT_DATASETS["us_equity"])
    return frozenset(dataset for datasets in REPORT_DATASETS.values() for dataset in datasets)


async def _load_frozen(
    database: AsyncSession, freeze_id: uuid.UUID
) -> tuple[FrozenObservation, ...]:
    edges = (
        await database.execute(
            select(
                ProjectionInputObservation.observation_kind,
                ProjectionInputObservation.observation_id,
            ).where(ProjectionInputObservation.freeze_id == freeze_id)
        )
    ).all()
    market_ids = [observation_id for kind, observation_id in edges if kind == "market"]
    rate_ids = [observation_id for kind, observation_id in edges if kind == "rate"]
    market_rows = (
        (
            await database.execute(
                select(MarketDailyObservation, MarketDailySeries)
                .join(MarketDailySeries)
                .where(MarketDailyObservation.id.in_(market_ids))
            )
        ).all()
        if market_ids
        else []
    )
    rate_rows = (
        (
            await database.execute(
                select(InterestRateObservation, InterestRateSeries)
                .join(InterestRateSeries)
                .where(InterestRateObservation.id.in_(rate_ids))
            )
        ).all()
        if rate_ids
        else []
    )
    loaded = {
        ("market", observation.id): (observation, series) for observation, series in market_rows
    }
    loaded.update(
        {("rate", observation.id): (observation, series) for observation, series in rate_rows}
    )
    result: list[FrozenObservation] = []
    for kind, observation_id in edges:
        observation, series = loaded[(kind, observation_id)]
        value = observation.close if kind == "market" else observation.value
        result.append(
            FrozenObservation(
                kind=kind,
                id=observation.id,
                function_attempt_id=observation.function_attempt_id,
                provider_key=series.provider_key,
                dataset_key=series.dataset_key,
                symbol=series.symbol,
                unit=series.unit,
                observation_date=observation.observation_date,
                value=value,
                open_value=observation.open if kind == "market" else None,
                is_provisional=(observation.is_provisional if kind == "market" else False),
                value_digest=observation.value_digest,
            )
        )
    return tuple(result)


async def publish_market_reports(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_run_id: uuid.UUID,
    owner: str,
    fence_token: uuid.UUID,
    frozen: tuple[FrozenObservation, ...],
) -> dict[str, Any]:
    actions: dict[str, str] = {}
    async with session_factory.begin() as database:
        job = await database.scalar(
            select(JobRun)
            .where(
                JobRun.id == job_run_id,
                JobRun.status == "running",
                JobRun.lease_owner == owner,
                JobRun.lease_token == fence_token,
            )
            .with_for_update()
        )
        if job is None:
            raise ValueError("projection lease is no longer current")
        current_cutoff = await database.scalar(
            select(ProjectionInputFreeze.cutoff_at).where(
                ProjectionInputFreeze.projection_job_run_id == job.id
            )
        )
        if current_cutoff is None:
            raise ValueError("projection inputs have not been frozen")
        frozen_outcomes = await _frozen_function_outcomes(database, job.id)
        requested = (job.payload or {}).get("requested_market_job")
        markets = (
            ("global_macro_bonds",)
            if requested == "global_macro_refresh"
            else ("us_equity",)
            if requested == "us_equity_refresh"
            else tuple(REPORT_DATASETS)
        )
        market_statuses: dict[str, str] = {}
        for market_code in markets:
            relevant_outcomes = tuple(
                outcome
                for outcome in frozen_outcomes
                if outcome.get("function_key") in REPORT_DATASETS[market_code]
            )
            rows = _rows_for_current_outcomes(
                tuple(row for row in frozen if row.dataset_key in REPORT_DATASETS[market_code]),
                relevant_outcomes,
            )
            digest = _rows_digest(market_code, rows, relevant_outcomes)
            await database.execute(
                select(func.pg_advisory_xact_lock(_publication_lock(market_code, job.edition_date)))
            )
            latest = await database.scalar(
                select(ReportPublication)
                .where(
                    ReportPublication.report_key == "daily-market",
                    ReportPublication.market_code == market_code,
                    ReportPublication.edition_date == job.edition_date,
                )
                .order_by(ReportPublication.revision.desc())
                .limit(1)
                .with_for_update()
            )
            if latest is not None and latest.input_digest == digest:
                actions[market_code] = "no_change"
                market_statuses[market_code] = str(latest.content.get("status", "unavailable"))
                continue
            latest_cutoff = (
                await database.scalar(
                    select(ProjectionInputFreeze.cutoff_at).where(
                        ProjectionInputFreeze.projection_job_run_id == latest.projection_job_run_id
                    )
                )
                if latest is not None and latest.projection_job_run_id is not None
                else None
            )
            if latest is not None and (latest_cutoff is None or latest_cutoff >= current_cutoff):
                actions[market_code] = "superseded"
                market_statuses[market_code] = str(latest.content.get("status", "unavailable"))
                continue
            bundle = _report_bundle(cast(LaunchMarketCode, market_code), rows)
            publication = ReportPublication(
                pipeline_run_id=None,
                projection_job_run_id=job.id,
                report_key="daily-market",
                market_code=market_code,
                edition_date=job.edition_date,
                revision=1 if latest is None else latest.revision + 1,
                derivation_version=MORNING_REPORT_DERIVATION_VERSION,
                content_schema_version=bundle.content.schema_version,
                input_digest=digest,
                source_as_of=bundle.content.as_of,
                manifest_version=ACTIVE_LAUNCH_MANIFEST.version,
                manifest_hash=ACTIVE_LAUNCH_MANIFEST.sha256,
                content=bundle.content_for_storage(),
                presentations=bundle.presentations_for_storage(),
            )
            database.add(publication)
            await database.flush()
            database.add_all(
                PublicationFunctionAttempt(
                    publication_id=publication.id, function_attempt_id=attempt_id
                )
                for attempt_id in (
                    {row.function_attempt_id for row in rows}
                    | _outcome_attempt_ids(relevant_outcomes)
                )
            )
            actions[market_code] = "published"
            market_statuses[market_code] = bundle.content.status
    return {
        "markets": actions,
        "market_statuses": market_statuses,
        "partial": any(status != "complete" for status in market_statuses.values()),
    }


def _symbol_history(
    rows: tuple[FrozenObservation, ...], dataset_key: str, symbol: str
) -> tuple[FrozenObservation, ...]:
    return tuple(
        sorted(
            (row for row in rows if row.dataset_key == dataset_key and row.symbol == symbol),
            key=lambda row: row.observation_date,
        )
    )


def _daily_bars(rows: tuple[FrozenObservation, ...]) -> tuple[DailyBar, ...]:
    return tuple(
        DailyBar(
            instrument_source_id=row.symbol,
            market="global_macro_bonds",
            symbol=row.symbol,
            trade_date=row.observation_date,
            open=row.open_value,
            close=row.value,
            source=row.provider_key,
        )
        for row in rows
    )


def _error_report_blocks(
    market_code: LaunchMarketCode, dataset_key: str
) -> tuple[ReportBlock, ...]:
    return _error_blocks_for_dataset(market_code, dataset_key)


def _report_bundle(
    market_code: LaunchMarketCode, rows: tuple[FrozenObservation, ...]
) -> PublicationBundle:
    blocks: list[ReportBlock] = []
    if market_code == "global_macro_bonds":
        commodity_symbols = tuple(symbol for _, symbol, _, _ in COMMODITIES)
        commodity_histories = {
            symbol: _symbol_history(rows, "commodity_daily_bars", symbol)
            for symbol in commodity_symbols
        }
        try:
            if any(len(history) < 2 for history in commodity_histories.values()):
                raise ValueError("commodity history is incomplete")
            identifiers = {symbol: identifier for identifier, symbol, _, _ in COMMODITIES}
            latest = {symbol: history[-1] for symbol, history in commodity_histories.items()}
            blocks.append(
                MetricBlock(
                    id="macro.commodities",
                    status="ok",
                    source_as_of=min(row.observation_date for row in latest.values()),
                    metrics=tuple(
                        MetricItem(
                            id=identifiers[symbol],
                            value=_quantize(
                                history[-1].value,
                                block_precision("macro.commodities"),
                                block_rounding("macro.commodities"),
                            ),
                            change=_quantize(
                                _previous_close_change(history[-1].value, history[-2].value),
                                block_precision("macro.commodities"),
                                block_rounding("macro.commodities"),
                            ),
                            unit_code=_identifier(history[-1].unit),
                        )
                        for symbol, history in commodity_histories.items()
                    ),
                )
            )
            ratio_histories = (
                _daily_bars(commodity_histories["WTI/USD"]),
                _daily_bars(commodity_histories["XAU/USD"]),
                _daily_bars(commodity_histories["HG1"]),
            )
            window_dates = _commodity_ratio_window_dates(ratio_histories)
            blocks.append(
                SeriesBlock(
                    id="macro.commodity_ratios",
                    status="ok",
                    source_as_of=window_dates[-1],
                    unit_code="ratio",
                    series=tuple(
                        ChartSeries(
                            id=identifier,
                            points=_ratio_common_date_points(
                                numerator,
                                ratio_histories[1],
                                window_dates,
                                precision=block_precision("macro.commodity_ratios"),
                                rounding=block_rounding("macro.commodity_ratios"),
                            ),
                        )
                        for identifier, numerator in (
                            ("oil_gold_ratio", ratio_histories[0]),
                            ("copper_gold_ratio", ratio_histories[2]),
                        )
                    ),
                )
            )
        except (DataSourceContractError, ValueError, ZeroDivisionError):
            blocks.extend(_error_report_blocks(market_code, "macro.commodity_eod"))

        rates_symbols = ("TLT", "IEF", "UUP", "USD/TWD", "USD/JPY", "EUR/USD")
        rates_histories = {
            symbol: _symbol_history(
                rows,
                "rates_proxy_daily_bars" if symbol in {"TLT", "IEF", "UUP"} else "fx_daily_bars",
                symbol,
            )
            for symbol in rates_symbols
        }
        if all(len(history) >= 2 for history in rates_histories.values()):
            blocks.append(
                MetricBlock(
                    id="macro.rates_fx",
                    status="ok",
                    source_as_of=min(
                        history[-1].observation_date for history in rates_histories.values()
                    ),
                    metrics=tuple(
                        MetricItem(
                            id=_identifier(symbol.replace("/", "_")),
                            value=_quantize(
                                history[-1].value,
                                block_precision("macro.rates_fx"),
                                block_rounding("macro.rates_fx"),
                            ),
                            change=_quantize(
                                _previous_close_change(history[-1].value, history[-2].value),
                                block_precision("macro.rates_fx"),
                                block_rounding("macro.rates_fx"),
                            ),
                            unit_code=_identifier(history[-1].unit),
                        )
                        for symbol, history in rates_histories.items()
                    ),
                )
            )
        else:
            blocks.extend(_error_report_blocks(market_code, "macro.rates_fx_daily_bars"))
        ordered = tuple(
            next(block for block in blocks if block.id == identifier)
            for identifier in (
                "macro.commodities",
                "macro.rates_fx",
                "macro.commodity_ratios",
            )
        )
        return _bundle(market_code, ordered)

    if market_code == "crypto":
        symbols: tuple[str, ...] = ("BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD")
        histories = {
            symbol: _symbol_history(rows, "crypto_daily_bars", symbol) for symbol in symbols
        }
        if all(
            len(history) >= 30 and history[-1].open_value is not None
            for history in histories.values()
        ):
            blocks.extend(
                (
                    TableBlock(
                        id="crypto.overview",
                        status="ok",
                        source_as_of=min(
                            history[-1].observation_date for history in histories.values()
                        ),
                        columns=(
                            TableColumn(id="asset"),
                            TableColumn(id="price", unit_code="usd"),
                            TableColumn(id="change", unit_code="percent"),
                        ),
                        rows=tuple(
                            (
                                TableCell(text=symbol.split("/")[0]),
                                TableCell(
                                    value=_quantize(
                                        history[-1].value,
                                        block_precision("crypto.overview"),
                                        block_rounding("crypto.overview"),
                                    )
                                ),
                                TableCell(
                                    value=_quantize(
                                        _daily_change(history[-1].open_value, history[-1].value),
                                        block_precision("crypto.overview"),
                                        block_rounding("crypto.overview"),
                                    )
                                ),
                            )
                            for symbol, history in histories.items()
                        ),
                    ),
                    SeriesBlock(
                        id="crypto.normalized_performance",
                        status="ok",
                        source_as_of=min(
                            history[-1].observation_date for history in histories.values()
                        ),
                        unit_code="index",
                        series=tuple(
                            ChartSeries(
                                id=symbol.split("/")[0].lower(),
                                points=_normalized_points(
                                    _daily_bars(history),
                                    precision=block_precision("crypto.normalized_performance"),
                                    rounding=block_rounding("crypto.normalized_performance"),
                                ),
                            )
                            for symbol, history in histories.items()
                        ),
                    ),
                )
            )
        else:
            blocks.extend(_error_report_blocks(market_code, "crypto.daily_bars"))
        return _bundle(market_code, tuple(blocks))

    symbols = ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA")
    histories = {
        symbol: _symbol_history(rows, "us_mega_cap_daily_bars", symbol) for symbol in symbols
    }
    if all(len(history) >= 2 for history in histories.values()):
        ranked = sorted(
            histories.items(),
            key=lambda item: _previous_close_change(item[1][-1].value, item[1][-2].value),
            reverse=True,
        )
        blocks.append(
            TableBlock(
                id="us.mega_caps",
                status="ok",
                source_as_of=min(history[-1].observation_date for history in histories.values()),
                columns=(
                    TableColumn(id="instrument"),
                    TableColumn(id="price", unit_code="usd"),
                    TableColumn(id="change", unit_code="percent"),
                ),
                rows=tuple(
                    (
                        TableCell(text=symbol),
                        TableCell(
                            value=_quantize(
                                history[-1].value,
                                block_precision("us.mega_caps"),
                                block_rounding("us.mega_caps"),
                            )
                        ),
                        TableCell(
                            value=_quantize(
                                _previous_close_change(history[-1].value, history[-2].value),
                                block_precision("us.mega_caps"),
                                block_rounding("us.mega_caps"),
                            )
                        ),
                    )
                    for symbol, history in ranked
                ),
            )
        )
    else:
        blocks.extend(_error_report_blocks(market_code, "us.mega_cap_daily_bars"))
    return _bundle(market_code, tuple(blocks))


async def publish_macro_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_run_id: uuid.UUID,
    owner: str,
    fence_token: uuid.UUID,
    frozen: tuple[FrozenObservation, ...],
) -> dict[str, Any]:
    datasets = set(MACRO_DATASETS)
    async with session_factory() as database:
        job = await database.get(JobRun, job_run_id)
        if job is None:
            raise ValueError("projection job does not exist")
        edition_date = job.edition_date
        frozen_outcomes = await _frozen_function_outcomes(database, job_run_id)
    relevant_outcomes = tuple(
        outcome for outcome in frozen_outcomes if outcome.get("function_key") in MACRO_DATASETS
    )
    rows = _rows_for_current_outcomes(
        tuple(row for row in frozen if row.dataset_key in MACRO_DATASETS), relevant_outcomes
    )
    if not rows:
        return {
            "action": "preserved",
            "partial": True,
            "reason": "all_sources_unavailable",
            "function_outcomes": list(relevant_outcomes),
        }
    grouped: dict[tuple[str, str], list[FrozenObservation]] = {}
    for row in rows:
        grouped.setdefault((row.dataset_key, row.symbol), []).append(row)
    histories = []
    for dataset, identifier, symbol, unit, source in _macro_history_specs():
        values = grouped.get((dataset, symbol), [])
        histories.append(
            History(
                id=identifier,
                symbol=symbol,
                unit=unit,
                source=source,
                status="ok" if values else "unavailable",
                points=[
                    Point(date=item.observation_date, value=item.value)
                    for item in sorted(values, key=lambda item: item.observation_date)
                ],
                provisional_date=max(
                    (item.observation_date for item in values if item.is_provisional),
                    default=None,
                ),
            )
        )
    dashboard = MacroDashboard(
        fetched_at=datetime.now(UTC),
        histories=histories,
        calendar=Calendar(
            status="disabled",
            date=max(row.observation_date for row in rows),
            source="orchestration",
            events=[],
        ),
    )
    missing_histories = [history.id for history in histories if history.status != "ok"]
    validation_failures = _macro_publication_failures(
        histories,
        relevant_outcomes,
        edition_date=edition_date,
    )
    if validation_failures:
        return {
            "action": "preserved",
            "partial": True,
            "reason": "incomplete_sources",
            "failures": validation_failures,
            "missing_datasets": sorted(datasets - {row.dataset_key for row in rows}),
            "missing_histories": missing_histories,
            "function_outcomes": list(relevant_outcomes),
        }
    async with session_factory.begin() as database:
        job = await database.scalar(
            select(JobRun)
            .where(
                JobRun.id == job_run_id,
                JobRun.status == "running",
                JobRun.lease_owner == owner,
                JobRun.lease_token == fence_token,
            )
            .with_for_update()
        )
        if job is None:
            raise ValueError("projection lease is no longer current")
        payload = dashboard.model_dump(mode="json")
        input_digest = _rows_digest("macro_dashboard", rows, relevant_outcomes)
        source_references = [
            {
                "provider": row.provider_key,
                "dataset": row.dataset_key,
                "symbol": row.symbol,
                "date": row.observation_date.isoformat(),
                "digest": row.value_digest,
                "function_attempt_id": str(row.function_attempt_id),
            }
            for row in rows
        ]
        await database.execute(select(func.pg_advisory_xact_lock(MACRO_DASHBOARD_PUBLICATION_LOCK)))
        existing = await database.scalar(
            select(MacroDashboardSnapshot)
            .where(MacroDashboardSnapshot.scope_key == "global_macro_bonds")
            .with_for_update()
        )
        if existing is not None and existing.input_digest == input_digest:
            missing = datasets - {row.dataset_key for row in rows}
            return {
                "action": "no_change",
                "missing_datasets": sorted(missing),
                "missing_histories": missing_histories,
                "partial": bool(missing_histories),
            }
        current_cutoff = await database.scalar(
            select(ProjectionInputFreeze.cutoff_at).where(
                ProjectionInputFreeze.projection_job_run_id == job.id
            )
        )
        if current_cutoff is None:
            raise ValueError("projection inputs have not been frozen")
        existing_cutoff = (
            await database.scalar(
                select(ProjectionInputFreeze.cutoff_at).where(
                    ProjectionInputFreeze.projection_job_run_id == existing.projection_job_run_id
                )
            )
            if existing is not None and existing.projection_job_run_id is not None
            else None
        )
        if existing is not None and (
            existing.edition_date > job.edition_date
            or (
                existing.edition_date == job.edition_date
                and (existing_cutoff is None or existing_cutoff >= current_cutoff)
            )
        ):
            missing = datasets - {row.dataset_key for row in rows}
            return {
                "action": "superseded",
                "missing_datasets": sorted(missing),
                "missing_histories": missing_histories,
                "partial": bool(missing_histories),
            }
        if existing is None:
            database.add(
                MacroDashboardSnapshot(
                    scope_key="global_macro_bonds",
                    fetched_at=dashboard.fetched_at,
                    edition_date=job.edition_date,
                    payload=payload,
                    input_digest=input_digest,
                    source_references=source_references,
                    projection_job_run_id=job.id,
                )
            )
        else:
            existing.fetched_at = dashboard.fetched_at
            existing.edition_date = job.edition_date
            existing.payload = payload
            existing.input_digest = input_digest
            existing.source_references = source_references
            existing.projection_job_run_id = job.id
            existing.updated_at = datetime.now(UTC)
    missing = datasets - {row.dataset_key for row in rows}
    return {
        "action": "published",
        "missing_datasets": sorted(missing),
        "missing_histories": missing_histories,
        "partial": bool(missing_histories),
    }


def _macro_publication_failures(
    histories: list[History],
    outcomes: tuple[dict[str, Any], ...],
    *,
    edition_date: date,
) -> list[dict[str, object]]:
    """Return stable diagnostics when a macro snapshot is unsafe to publish."""
    failures: list[dict[str, object]] = []
    outcome_by_dataset = {
        str(outcome.get("function_key")): outcome
        for outcome in outcomes
        if outcome.get("function_key")
    }
    for dataset in sorted(MACRO_DATASETS):
        outcome = outcome_by_dataset.get(dataset)
        status = outcome.get("status") if outcome is not None else None
        if status not in MACRO_AVAILABLE_STATUSES:
            failures.append(
                {
                    "type": "source_unavailable",
                    "dataset": dataset,
                    "affected_items": sorted(
                        str(value)
                        for value in (outcome or {}).get("missing_scopes", [])
                        if isinstance(value, str)
                    ),
                }
            )

    for history in histories:
        if history.status != "ok" or not history.points:
            failures.append(
                {
                    "type": "source_unavailable",
                    "dataset": _history_dataset(history),
                    "affected_items": [history.symbol],
                }
            )
            continue
        newest = history.points[-1].date
        if newest > edition_date or edition_date - newest > MACRO_MAX_HISTORY_AGE:
            failures.append(
                {
                    "type": "stale_data",
                    "dataset": _history_dataset(history),
                    "affected_items": [history.symbol],
                }
            )
        if history.points[0].date > newest - MACRO_MIN_HISTORY_SPAN:
            failures.append(
                {
                    "type": "insufficient_history",
                    "dataset": _history_dataset(history),
                    "affected_items": [history.symbol],
                }
            )

    treasury = [history for history in histories if history.source == "U.S. Treasury"]
    if len(treasury) == len(TENORS) and all(history.points for history in treasury):
        common_dates = {point.date for point in treasury[0].points}
        for history in treasury[1:]:
            common_dates.intersection_update(point.date for point in history.points)
        if not common_dates or edition_date - max(common_dates) > MACRO_MAX_HISTORY_AGE:
            failures.append(
                {
                    "type": "stale_data",
                    "dataset": "treasury_yield_curve",
                    "affected_items": sorted(history.symbol for history in treasury),
                }
            )
    return failures


def _history_dataset(history: History) -> str:
    if history.source == "Twelve Data":
        return (
            "fx_daily_bars"
            if history.symbol in {item[1] for item in FX_INSTRUMENTS}
            else "commodity_daily_bars"
        )
    if history.source == "Yahoo Finance":
        return "dxy_daily_bars"
    if history.source == "U.S. Treasury":
        return "treasury_yield_curve"
    return "sofr_daily_rates"


def _macro_payload_content(payload: dict[str, Any]) -> dict[str, Any]:
    comparable = dict(payload)
    comparable.pop("fetched_at", None)
    return comparable


def _macro_history_identity(dataset: str, symbol: str, unit: str) -> tuple[str, str]:
    if dataset == "commodity_daily_bars":
        for identifier, source_symbol, canonical_unit, _ in COMMODITIES:
            if symbol == source_symbol:
                return identifier, canonical_unit
    if dataset == "treasury_yield_curve":
        for identifier, source_symbol in TENORS:
            if symbol == source_symbol:
                return identifier, "percent"
    if dataset == "sofr_daily_rates":
        return "sofr", "percent"
    if dataset == "dxy_daily_bars":
        return INSTRUMENTS[0][0], INSTRUMENTS[0][2]
    if dataset == "fx_daily_bars":
        for identifier, source_symbol, canonical_unit in FX_INSTRUMENTS:
            if symbol == source_symbol:
                return identifier, canonical_unit
    return _identifier(symbol), unit


def _macro_history_specs() -> tuple[tuple[str, str, str, str, str], ...]:
    return (
        *(
            ("commodity_daily_bars", identifier, symbol, unit, "Twelve Data")
            for identifier, symbol, unit, _ in COMMODITIES
        ),
        *(
            ("dxy_daily_bars", identifier, symbol, unit, "Yahoo Finance")
            for identifier, symbol, unit in INSTRUMENTS
        ),
        *(
            ("fx_daily_bars", identifier, symbol, unit, "Twelve Data")
            for identifier, symbol, unit in FX_INSTRUMENTS
        ),
        *(
            ("treasury_yield_curve", identifier, symbol, "percent", "U.S. Treasury")
            for identifier, symbol in TENORS
        ),
        ("sofr_daily_rates", "sofr", "SOFR", "percent", "New York Fed"),
    )


def _rows_digest(
    scope: str,
    rows: tuple[FrozenObservation, ...],
    function_outcomes: tuple[dict[str, Any], ...] = (),
) -> str:
    material = "|".join(
        sorted(
            f"{row.provider_key}:{row.dataset_key}:{row.symbol}:"
            f"{row.observation_date.isoformat()}:{row.value_digest}"
            for row in rows
        )
    )
    identity = ":".join(
        (
            "typed-facts.v2",
            MORNING_REPORT_DERIVATION_VERSION,
            ACTIVE_LAUNCH_MANIFEST.version,
            ACTIVE_LAUNCH_MANIFEST.sha256,
            scope,
        )
    )
    semantic_outcomes = []
    for outcome in function_outcomes:
        semantic = {key: value for key, value in outcome.items() if key != "attempt_ids"}
        if semantic.get("status") in {"succeeded", "no_change"}:
            semantic["status"] = "available"
        semantic_outcomes.append(semantic)
    outcome_material = json.dumps(semantic_outcomes, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{identity}|{material}|{outcome_material}".encode()).hexdigest()


def _identifier(value: str) -> str:
    normalized = "".join(character.lower() if character.isalnum() else "." for character in value)
    normalized = ".".join(filter(None, normalized.split(".")))
    return normalized if normalized and normalized[0].isalpha() else f"v.{normalized or 'unknown'}"


def _publication_lock(market_code: str, edition_date: date) -> int:
    digest = hashlib.sha256(f"publication:{market_code}:{edition_date}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)
