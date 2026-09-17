import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from typing import Final, Protocol

import httpx
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.analyst_viewpoints.models import (
    AnalystViewpoint,
    AnalystViewpointSyncRun,
    AnalystViewpointVersion,
)
from daily_insights_api.modules.analyst_viewpoints.schemas import (
    AnalystViewpointExecutionResponse,
    AnalystViewpointResponse,
    AnalystViewpointSyncResponse,
    SyncMarketStatus,
    UpstreamSummary,
)
from daily_insights_api.modules.orchestration.api import FunctionAttempt, FunctionRun

MARKET_MAPPING: Final[dict[str, str]] = {
    "us_macro": "global_macro_bonds",
    "forex": "forex",
    "crypto": "crypto",
    "us_stocks": "us_equity",
    "hk_stocks": "hk_equity",
    "cn_stocks": "cn_equity",
    "tw_stocks": "tw_equity",
    "tw_futures": "tw_index_derivatives",
}


class AnalystViewpointSyncError(RuntimeError):
    """A safe failure used by the scheduler and manual administration endpoint."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class AnalystViewpointReader(Protocol):
    async def fetch_summary(self) -> tuple[UpstreamSummary, datetime]: ...


class AnalystViewpointClient:
    """A deliberately narrow, read-only upstream client."""

    def __init__(self, *, base_url: str, api_key: SecretStr, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def fetch_summary(self) -> tuple[UpstreamSummary, datetime]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.get(
                    "/api/summary", headers={"X-API-Key": self._api_key.get_secret_value()}
                )
                response.raise_for_status()
                raw = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AnalystViewpointSyncError(
                "upstream analyst viewpoints are unavailable", code="upstream_unavailable"
            ) from error
        try:
            return UpstreamSummary.model_validate(raw), datetime.now(UTC)
        except ValidationError as error:
            raise AnalystViewpointSyncError(
                "upstream analyst viewpoints failed validation", code="upstream_invalid_response"
            ) from error


async def list_viewpoints(
    database: AsyncSession, viewpoint_date: date, *, market_codes: set[str] | None = None
) -> list[AnalystViewpointResponse]:
    statement = select(AnalystViewpoint).where(AnalystViewpoint.viewpoint_date == viewpoint_date)
    if market_codes is not None:
        statement = statement.where(AnalystViewpoint.market_code.in_(market_codes))
    rows = (await database.scalars(statement)).all()
    by_market = {row.market_code: row for row in rows}
    return [
        AnalystViewpointResponse(
            viewpoint_date=row.viewpoint_date,
            market_code=row.market_code,
            source_market_code=row.source_market_code,
            points=row.points,
            fetched_at=row.fetched_at,
        )
        for market in MARKET_MAPPING.values()
        if (row := by_market.get(market)) is not None
    ]


async def sync_viewpoints(
    database: AsyncSession,
    client: AnalystViewpointReader,
    viewpoint_date: date,
    *,
    function_attempt_id: uuid.UUID | None = None,
    fence_token: uuid.UUID | None = None,
) -> AnalystViewpointSyncResponse:
    """Persist only present valid values; an empty/failed market never deletes a prior value."""

    summary, fetched_at = await client.fetch_summary()
    statuses: list[SyncMarketStatus] = []
    for source_market_code, market_code in MARKET_MAPPING.items():
        points = getattr(summary, source_market_code)
        if not points:
            statuses.append(
                SyncMarketStatus(
                    source_market_code=source_market_code, market_code=market_code, status="missing"
                )
            )
            continue
        current_version_id: uuid.UUID | None = None
        if function_attempt_id is not None:
            if fence_token is None or not await _analyst_fence_is_current(
                database, function_attempt_id, fence_token
            ):
                statuses.append(
                    SyncMarketStatus(
                        source_market_code=source_market_code,
                        market_code=market_code,
                        status="stale",
                    )
                )
                continue
            version_number = (
                await database.scalar(
                    select(func.max(AnalystViewpointVersion.version)).where(
                        AnalystViewpointVersion.viewpoint_date == viewpoint_date,
                        AnalystViewpointVersion.market_code == market_code,
                    )
                )
                or 0
            ) + 1
            digest = hashlib.sha256(
                json.dumps(points, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
            version = AnalystViewpointVersion(
                viewpoint_date=viewpoint_date,
                market_code=market_code,
                source_market_code=source_market_code,
                version=version_number,
                points=points,
                fetched_at=fetched_at,
                content_digest=digest,
                function_attempt_id=function_attempt_id,
            )
            database.add(version)
            await database.flush()
            current_version_id = version.id
        statement = insert(AnalystViewpoint).values(
            viewpoint_date=viewpoint_date,
            market_code=market_code,
            source_market_code=source_market_code,
            points=points,
            fetched_at=fetched_at,
            current_version_id=current_version_id,
        )
        update_values = {
            "source_market_code": statement.excluded.source_market_code,
            "points": statement.excluded.points,
            "fetched_at": statement.excluded.fetched_at,
        }
        if function_attempt_id is not None:
            update_values["current_version_id"] = statement.excluded.current_version_id
        upsert_statement = statement.on_conflict_do_update(
            constraint="uq_analyst_viewpoint_date_market",
            set_=update_values,
            where=statement.excluded.fetched_at >= AnalystViewpoint.fetched_at,
        ).returning(AnalystViewpoint.id)
        result = await database.execute(upsert_statement)
        wrote_viewpoint = result.scalar_one_or_none() is not None
        statuses.append(
            SyncMarketStatus(
                source_market_code=source_market_code,
                market_code=market_code,
                status="updated" if wrote_viewpoint else "stale",
            )
        )
    return AnalystViewpointSyncResponse(
        viewpoint_date=viewpoint_date,
        fetched_at=fetched_at,
        status="complete" if all(item.status == "updated" for item in statuses) else "partial",
        markets=statuses,
    )


def _execution_response(run: AnalystViewpointSyncRun) -> AnalystViewpointExecutionResponse:
    return AnalystViewpointExecutionResponse(
        viewpoint_date=run.viewpoint_date,
        trigger=run.trigger,
        status=run.status,
        fetched_at=run.fetched_at,
        completed_at=run.completed_at,
        error_code=run.error_code,
        markets=[SyncMarketStatus.model_validate(item) for item in run.markets],
    )


async def record_sync_execution(
    database: AsyncSession,
    *,
    viewpoint_date: date,
    trigger: str,
    result: AnalystViewpointSyncResponse | None = None,
    error_code: str | None = None,
    function_attempt_id: uuid.UUID | None = None,
) -> AnalystViewpointExecutionResponse:
    """Persist a scheduler-safe execution result without an identity dependency."""

    run = AnalystViewpointSyncRun(
        viewpoint_date=viewpoint_date,
        trigger=trigger,
        status=result.status if result is not None else "failed",
        fetched_at=result.fetched_at if result is not None else None,
        completed_at=datetime.now(UTC),
        error_code=error_code,
        markets=(
            [item.model_dump(mode="json") for item in result.markets] if result is not None else []
        ),
        function_attempt_id=function_attempt_id,
    )
    database.add(run)
    await database.flush()
    return _execution_response(run)


async def _analyst_fence_is_current(
    database: AsyncSession, function_attempt_id: uuid.UUID, fence_token: uuid.UUID
) -> bool:
    return bool(
        await database.scalar(
            select(FunctionRun.id)
            .join(FunctionAttempt, FunctionAttempt.function_run_id == FunctionRun.id)
            .where(
                FunctionAttempt.id == function_attempt_id,
                FunctionAttempt.fence_token == fence_token,
                FunctionAttempt.status == "running",
                FunctionRun.status == "running",
                FunctionRun.lease_token == fence_token,
            )
            .with_for_update(of=FunctionRun)
        )
    )


async def latest_sync_execution(
    database: AsyncSession,
) -> AnalystViewpointExecutionResponse | None:
    run = await database.scalar(
        select(AnalystViewpointSyncRun)
        .order_by(AnalystViewpointSyncRun.completed_at.desc())
        .limit(1)
    )
    return _execution_response(run) if run is not None else None
