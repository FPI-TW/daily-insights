"""Durable news recovery. Transactions never span external calls.

The queue supplies ownership and scheduling; this module owns checkpoints and
dependency health. Stored values are metadata or validated model outputs only.
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.news.contracts import LocalizedSummary, SelectedCandidate, Selection
from daily_insights_api.modules.news.failures import (
    NewsFailure,
    NewsOperationError,
    NewsStage,
    classify_failure,
    retry_time,
)
from daily_insights_api.modules.news.llm import ModelCall
from daily_insights_api.modules.news.models import (
    NewsCheckpoint,
    NewsDependencyState,
    NewsWorkflow,
)

TAIPEI = ZoneInfo("Asia/Taipei")
PROVIDER_SCOPE = "provider:news"
SessionFactory = async_sessionmaker[AsyncSession]


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def automatic_window(now: datetime, edition_date: date) -> bool:
    local = now.astimezone(TAIPEI)
    return local.date() == edition_date and time(8) <= local.time() < time(12)


async def cleanup_checkpoints(sessions: SessionFactory, now: datetime) -> None:
    """Run independently of the paid-call window; preserve editions and audits."""
    async with sessions.begin() as database:
        await database.execute(delete(NewsCheckpoint).where(NewsCheckpoint.expires_at <= now))


@asynccontextmanager
async def exclusive(sessions: SessionFactory, key: str) -> AsyncIterator[None]:
    """Session lock, deliberately NOT a transaction held during provider I/O."""
    number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
    async with sessions() as database:
        # Pin the connection: committing a Session would return its connection
        # (and its still-held advisory lock) to the pool.
        connection = await database.connection()
        await connection.execute(select(func.pg_advisory_lock(number)))
        await connection.commit()
        try:
            yield
        finally:
            await connection.execute(select(func.pg_advisory_unlock(number)))
            await connection.commit()


@dataclass
class NewsExecution:
    sessions: SessionFactory
    root_run_id: uuid.UUID
    run_id: uuid.UUID
    edition_date: date
    automatic: bool = False
    attempt: int = 0
    guard: Callable[[], Awaitable[bool]] | None = None
    guard_transaction: Callable[[AsyncSession], Awaitable[bool]] | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


_execution: ContextVar[NewsExecution | None] = ContextVar("news_execution", default=None)
_workflow: ContextVar["Workflow | None"] = ContextVar("news_workflow", default=None)


@contextmanager
def news_execution(execution: NewsExecution) -> Iterator[None]:
    token = _execution.set(execution)
    try:
        yield
    finally:
        _execution.reset(token)


def current_workflow() -> "Workflow | None":
    return _workflow.get()


@dataclass
class Workflow:
    execution: NewsExecution
    id: uuid.UUID
    market: str
    failures: list[NewsFailure] = field(default_factory=list)
    progress: dict[str, int] = field(default_factory=dict)
    stage: NewsStage = "queued"
    model_stopped: NewsFailure | None = None
    resuming: bool = False
    completed_steps: set[str] = field(default_factory=set)

    async def fence_publication(self, database: AsyncSession) -> None:
        guard = self.execution.guard_transaction
        if guard is not None and not await guard(database):
            raise NewsOperationError(
                NewsFailure(code="ownership_lost", action="cancelled", stage="publication")
            )

    async def completed(self, key: str, stage: NewsStage) -> None:
        if key not in self.completed_steps:
            self.completed_steps.add(key)
            self.progress[stage] = self.progress.get(stage, 0) + 1
        await self.save()

    async def save(self, *, state: str = "running") -> None:
        async with self.execution.sessions.begin() as database:
            row = await database.get(NewsWorkflow, self.id, with_for_update=True)
            assert row is not None
            row.run_id = self.execution.run_id
            row.state, row.stage = state, self.stage
            row.progress = dict(self.progress)
            row.failures = [failure.model_dump(mode="json") for failure in self.failures]
            row.attempt = self.execution.attempt
            row.next_retry_at = self.next_retry_at() if self.execution.automatic else None
            row.updated_at = self.execution.clock()

    def next_retry_at(self) -> datetime | None:
        retryable = [failure for failure in self.failures if failure.action == "retry"]
        if not retryable:
            return None
        return max(
            retry_time(self.execution.attempt, self.execution.clock(), failure.retry_after)
            for failure in retryable
        )

    async def check(self, stage: NewsStage, *, external: bool = True) -> None:
        self.stage = stage
        if self.execution.guard is not None and not await self.execution.guard():
            raise NewsOperationError(
                NewsFailure(code="ownership_lost", action="cancelled", stage=stage)
            )
        if (
            external
            and self.execution.automatic
            and not automatic_window(self.execution.clock(), self.execution.edition_date)
        ):
            raise NewsOperationError(
                NewsFailure(code="news_window_expired", action="expired", stage=stage)
            )
        # A failed database write stops before any further paid request.
        await self.save()

    async def record(self, failure: NewsFailure) -> None:
        if failure.request_id is None:
            failure = failure.model_copy(update={"request_id": str(self.execution.run_id)})
        emit_event(
            "news.recovery.failure",
            run_id=str(self.execution.run_id),
            market=self.market,
            stage=failure.stage,
            scope=failure.scope,
            error_code=failure.code,
            action=failure.action,
            request_id=failure.request_id,
        )
        identity = (failure.scope, failure.stage, failure.candidate_id, failure.locale)
        self.failures = [
            item
            for item in self.failures
            if (item.scope, item.stage, item.candidate_id, item.locale) != identity
        ] + [failure]
        if failure.scope == PROVIDER_SCOPE and failure.action in {
            "retry",
            "block",
            "attention",
            "expired",
            "cancelled",
        }:
            self.model_stopped = failure
        await self.save()

    async def checkpoint(self, key: str, stage: NewsStage) -> NewsCheckpoint:
        async with self.execution.sessions.begin() as database:
            await database.execute(
                insert(NewsCheckpoint)
                .values(
                    workflow_id=self.id,
                    key=key,
                    stage=stage,
                    repairs=0,
                    expires_at=self.execution.clock() + timedelta(hours=48),
                )
                .on_conflict_do_nothing()
            )
            row = await database.scalar(
                select(NewsCheckpoint).where(
                    NewsCheckpoint.workflow_id == self.id, NewsCheckpoint.key == key
                )
            )
            assert row is not None
            return row

    async def store(self, checkpoint: NewsCheckpoint) -> None:
        async with self.execution.sessions.begin() as database:
            await database.merge(checkpoint)

    async def finish(self) -> None:
        if (
            self.progress.get("feeds_ok") == 0
            and any(failure.stage == "feed" for failure in self.failures)
            and not any(
                failure.action in {"retry", "expired", "cancelled"} for failure in self.failures
            )
        ):
            self.failures.append(
                NewsFailure(code="all_sources_unavailable", action="attention", stage="feed")
            )
        actions = {failure.action for failure in self.failures}
        state = (
            "cancelled"
            if "cancelled" in actions
            else "expired"
            if "expired" in actions
            else "needs_attention"
            if actions & {"block", "attention"}
            else "waiting_retry"
            if "retry" in actions
            else "completed"
        )
        if state == "waiting_retry" and self.execution.automatic:
            due = self.next_retry_at()
            if due is not None and not automatic_window(due, self.execution.edition_date):
                state = "expired"
        elif state == "waiting_retry":
            state = "needs_attention"
        if state == "completed":
            self.stage = "complete"
        await self.save(state=state)


@asynccontextmanager
async def workflow_scope(
    sessions: SessionFactory, edition_date: date, market: str
) -> AsyncIterator[Workflow]:
    execution = _execution.get() or NewsExecution(
        sessions, uuid.uuid4(), uuid.uuid4(), edition_date
    )
    async with exclusive(sessions, f"news-workflow:{edition_date}:{market}"):
        async with sessions.begin() as database:
            await database.execute(
                delete(NewsCheckpoint).where(NewsCheckpoint.expires_at < execution.clock())
            )
            await database.execute(
                insert(NewsWorkflow)
                .values(
                    root_run_id=execution.root_run_id,
                    run_id=execution.run_id,
                    edition_date=edition_date,
                    market_code=market,
                    state="running",
                    stage="queued",
                    progress={},
                    failures=[],
                    attempt=execution.attempt,
                )
                .on_conflict_do_nothing()
            )
            row = await database.scalar(
                select(NewsWorkflow).where(
                    NewsWorkflow.root_run_id == execution.root_run_id,
                    NewsWorkflow.market_code == market,
                )
            )
            assert row is not None
            workflow = Workflow(
                execution, row.id, market, resuming=bool(row.progress or row.failures)
            )
        token = _workflow.set(workflow)
        try:
            await workflow.check("feed")
            yield workflow
        except asyncio.CancelledError:
            owned = execution.guard is None or await execution.guard()
            await workflow.record(
                NewsFailure(
                    code="worker_interrupted" if owned else "ownership_lost",
                    action="retry" if owned else "cancelled",
                    stage=workflow.stage,
                )
            )
            raise
        except Exception as error:
            await workflow.record(classify_failure(error, stage=workflow.stage))
            raise
        finally:
            try:
                await workflow.finish()
            finally:
                _workflow.reset(token)


async def dependency_failure(
    sessions: SessionFactory, failure: NewsFailure, *, now: datetime
) -> None:
    async with sessions.begin() as database:
        await database.execute(
            insert(NewsDependencyState)
            .values(scope=failure.scope, state="ready")
            .on_conflict_do_nothing()
        )
        row = await database.get(NewsDependencyState, failure.scope, with_for_update=True)
        assert row is not None
        row.failure = failure.model_dump(mode="json")
        row.state = (
            "blocked"
            if failure.action == "block"
            else "cooldown"
            if failure.action == "retry"
            else "attention"
        )
        row.available_at = (
            retry_time(row.failures_count, now, failure.retry_after)
            if failure.action == "retry"
            else None
        )
        row.failures_count += 1
        row.probe_run_id = None
        row.updated_at = now


async def check_dependency(workflow: Workflow, scope: str) -> None:
    async with workflow.execution.sessions() as database:
        state = await database.get(NewsDependencyState, scope)
        if state is None or state.state in {"ready", "no_new_content"}:
            return
        blocked = (
            state.state == "blocked"
            or (state.state == "probing" and state.probe_run_id != workflow.execution.run_id)
            or (state.available_at is not None and state.available_at > workflow.execution.clock())
        )
        if state.state == "attention" and scope == PROVIDER_SCOPE:
            blocked = True
        if blocked and state.failure is not None:
            failure = NewsFailure.model_validate(state.failure).model_copy(
                update={
                    "retry_after": state.available_at,
                }
            )
            await workflow.record(failure)
            raise NewsOperationError(failure)


async def source_failure(
    workflow: Workflow,
    checkpoint: NewsCheckpoint,
    error: Exception,
    *,
    stage: NewsStage,
    hostname: str,
    candidate_id: str | None = None,
) -> None:
    failure = classify_failure(
        error,
        stage=stage,
        scope=f"source:{hostname}",
        candidate_id=candidate_id,
        now=workflow.execution.clock(),
    )
    checkpoint.failure = failure.model_dump(mode="json")
    await workflow.store(checkpoint)
    await workflow.record(failure)
    if (
        not isinstance(error, NewsOperationError) or failure.code == "source_configuration_missing"
    ) and (failure.http_status == 429 or stage == "feed"):
        await dependency_failure(
            workflow.execution.sessions, failure, now=workflow.execution.clock()
        )


async def source_success(
    workflow: Workflow, scope: str, newest: datetime | None, count: int
) -> None:
    async with workflow.execution.sessions.begin() as database:
        await database.execute(
            insert(NewsDependencyState).values(scope=scope, state="ready").on_conflict_do_nothing()
        )
        row = await database.get(NewsDependencyState, scope, with_for_update=True)
        assert row is not None
        if (
            row.state == "cooldown"
            and row.available_at
            and row.available_at > workflow.execution.clock()
        ):
            # Another feed on this host may have returned 429 while this
            # already-in-flight request succeeded. Never erase its cooldown.
            return
        row.state = "ready" if count else "no_new_content"
        row.newest_article_at = newest
        row.failure, row.available_at = None, None
        row.failures_count = 0
        row.updated_at = workflow.execution.clock()


async def model_step(
    key: str,
    stage: NewsStage,
    call: Callable[[], Awaitable[ModelCall]],
    on_failure: Callable[[Exception], None],
    *,
    locale: str | None = None,
    candidate_id: str | None = None,
    after_failure: Callable[[], Awaitable[None]] | None = None,
) -> ModelCall:
    workflow = current_workflow()
    if workflow is None:
        for attempt in range(2):
            try:
                return await call()
            except Exception as error:
                on_failure(error)
                if after_failure is not None:
                    await after_failure()
                if attempt or classify_failure(error, stage=stage).action != "repair":
                    raise
        raise AssertionError("unreachable")
    await workflow.check(stage)
    checkpoint = await workflow.checkpoint(key, stage)
    if checkpoint.result is not None:
        await workflow.completed(key, stage)
        return _restore_call(checkpoint.result, stage)
    if checkpoint.failure is not None:
        failure = NewsFailure.model_validate(checkpoint.failure)
        if failure.action == "repair" and checkpoint.repairs >= 1:
            failure = failure.model_copy(
                update={
                    "action": "skip" if stage == "summary" else "attention",
                    "code": f"{failure.code}_exhausted",
                }
            )
            checkpoint.failure = failure.model_dump(mode="json")
            await workflow.store(checkpoint)
        if failure.action == "skip" or failure.code.endswith("_exhausted"):
            await workflow.record(failure)
            raise NewsOperationError(failure)
    if workflow.model_stopped is not None:
        raise NewsOperationError(workflow.model_stopped)
    async with exclusive(workflow.execution.sessions, PROVIDER_SCOPE):
        await workflow.check(stage)
        await check_dependency(workflow, PROVIDER_SCOPE)
        async with workflow.execution.sessions() as database:
            dependency = await database.get(NewsDependencyState, PROVIDER_SCOPE)
            probing = dependency is not None and dependency.state == "probing"
        while True:
            if checkpoint.failure is not None and checkpoint.failure.get("action") == "repair":
                # Reserve the one correction BEFORE dispatch; a crash must not
                # reset the budget or blindly submit a second correction.
                checkpoint.repairs += 1
                await workflow.store(checkpoint)
            await workflow.check(stage)
            try:
                result = await call()
            except Exception as error:
                on_failure(error)
                if after_failure is not None:
                    await after_failure()
                failure = classify_failure(
                    error,
                    stage=stage,
                    scope=PROVIDER_SCOPE,
                    candidate_id=candidate_id,
                    locale=locale,
                    now=workflow.execution.clock(),
                )
                if probing:
                    # One admin-authorized probe, not a hidden retry loop.
                    # Keep repair eligibility in the input checkpoint while
                    # the shared gate records the newly observed failure.
                    checkpoint.failure = failure.model_dump(mode="json")
                    await workflow.store(checkpoint)
                    if failure.action == "repair":
                        failure = failure.model_copy(update={"action": "attention"})
                    await dependency_failure(
                        workflow.execution.sessions, failure, now=workflow.execution.clock()
                    )
                    await workflow.record(failure)
                    raise NewsOperationError(failure) from error
                if failure.action == "repair":
                    if checkpoint.repairs >= 1:
                        failure = failure.model_copy(
                            update={
                                "action": "skip" if stage == "summary" else "attention",
                                "code": f"{failure.code}_exhausted",
                            }
                        )
                    checkpoint.failure = failure.model_dump(mode="json")
                    await workflow.store(checkpoint)
                    if failure.action == "repair":
                        continue
                else:
                    checkpoint.failure = failure.model_dump(mode="json")
                    await workflow.store(checkpoint)
                    if failure.action in {"retry", "block", "attention"}:
                        await dependency_failure(
                            workflow.execution.sessions, failure, now=workflow.execution.clock()
                        )
                await workflow.record(failure)
                raise NewsOperationError(failure) from error
            checkpoint.result = _store_call(result)
            checkpoint.failure = None
            await workflow.store(checkpoint)
            async with workflow.execution.sessions.begin() as database:
                dependency = await database.get(
                    NewsDependencyState, PROVIDER_SCOPE, with_for_update=True
                )
                if dependency is not None:
                    dependency.state, dependency.failure, dependency.available_at = (
                        "ready",
                        None,
                        None,
                    )
                    dependency.probe_run_id = None
                    dependency.failures_count = 0
                    dependency.updated_at = workflow.execution.clock()
            await workflow.completed(key, stage)
            return result


def _store_call(call: ModelCall) -> dict[str, Any]:
    return {
        "value": call.value.model_dump(mode="json"),
        "request_id": call.request_id,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "latency_ms": call.latency_ms,
        "input_digest": call.input_digest,
        "rejected": [[item.model_dump(mode="json"), reason] for item, reason in call.rejected],
        "returned": [item.model_dump(mode="json") for item in call.returned],
    }


def _restore_call(value: dict[str, Any], stage: NewsStage) -> ModelCall:
    return ModelCall(
        Selection.model_validate(value["value"])
        if stage == "selection"
        else LocalizedSummary.model_validate(value["value"]),
        value["request_id"],
        value["input_tokens"],
        value["output_tokens"],
        value["latency_ms"],
        value["input_digest"],
        tuple(
            (SelectedCandidate.model_validate(item), reason) for item, reason in value["rejected"]
        ),
        tuple(SelectedCandidate.model_validate(item) for item in value["returned"]),
        reused=True,
    )


async def workflow_results(sessions: SessionFactory, run_id: uuid.UUID) -> dict[str, Any]:
    async with sessions() as database:
        rows = (
            await database.scalars(select(NewsWorkflow).where(NewsWorkflow.run_id == run_id))
        ).all()
        return {
            row.market_code: {
                "id": str(row.id),
                "state": row.state,
                "stage": row.stage,
                "progress": row.progress,
                "failures": row.failures,
                "attempt": row.attempt,
                "next_retry_at": row.next_retry_at.isoformat() if row.next_retry_at else None,
                "publication": "technical_degradation"
                if row.failures
                else "editorial_shortfall"
                if row.progress.get("published", 0) < 5
                else "available",
            }
            for row in rows
        }
