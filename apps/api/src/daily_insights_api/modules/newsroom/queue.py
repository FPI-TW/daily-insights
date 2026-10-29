"""Row-as-queue claiming and settling for every newsroom stage (spec §5).

A row is claimable for ``stage`` when ``<stage>_status = 'pending'`` and
``<stage>_next_attempt_at`` is NULL or due. Claiming bumps the attempt counter
and pushes ``next_attempt_at`` out by the lease, so a crashed worker's claim
simply becomes due again. Settling is fenced on a unique claim token, separate
from retry accounting: resetting attempts never makes an old claim current.

Stages must not invent their own retry logic; they raise ``RetryableStageError``
or ``FatalStageError`` (or return normally) and ``run_claimed`` settles the row.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, event, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from daily_insights_api.modules.newsroom.models import (
    NewsroomArticle,
    NewsroomEditionItem,
    NewsroomEvent,
    NewsroomLlmCall,
)

QueueModel = type[NewsroomArticle] | type[NewsroomEvent] | type[NewsroomEditionItem]

DEFAULT_LEASE = timedelta(minutes=10)
MAX_BACKOFF = timedelta(minutes=60)


class RetryableStageError(Exception):
    """Transient failure: network, timeout, 429, 5xx, invalid JSON or schema.

    ``counts_attempt=False`` marks a wait rather than a failure, such as a spent
    provider allowance that comes back on its own: the row keeps its attempts
    and waits as often as it takes instead of running out of them.
    """

    def __init__(
        self, code: str, *, retry_after: timedelta | None = None, counts_attempt: bool = True
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.counts_attempt = counts_attempt
        # Audit rows for the failed call; re-added after the stage's rollback.
        self.audit_rows: list[Any] = []


class FatalStageError(Exception):
    """Failure that retrying cannot fix: 401/402/403, missing configuration."""

    def __init__(self, code: str, *, notify: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.notify = notify
        self.audit_rows: list[Any] = []


@dataclass(frozen=True, slots=True)
class StageSpec:
    """One queue: which table, which column prefix, and what success means."""

    name: str
    model: QueueModel
    column: str
    done_status: str
    max_attempts: int
    lease: timedelta = DEFAULT_LEASE


@dataclass(frozen=True, slots=True)
class Claim:
    stage: StageSpec
    row_id: uuid.UUID
    attempt: int
    token: uuid.UUID


def backoff(attempt: int, retry_after: timedelta | None = None) -> timedelta:
    """1, 2, 4, 8 ... minutes capped at 60, never shorter than Retry-After."""
    delay = min(timedelta(minutes=2 ** max(attempt - 1, 0)), MAX_BACKOFF)
    if retry_after is not None and retry_after > delay:
        return min(retry_after, MAX_BACKOFF * 6)
    return delay


def _col(stage: StageSpec, suffix: str) -> Any:
    return getattr(stage.model, f"{stage.column}_{suffix}")


async def claim(
    database: AsyncSession,
    stage: StageSpec,
    *,
    limit: int = 1,
    now: datetime | None = None,
    extra_filter: Any = None,
) -> list[Claim]:
    """Claim up to ``limit`` due rows and commit the lease."""
    moment = now or datetime.now(UTC)
    status = _col(stage, "status")
    next_at = _col(stage, "next_attempt_at")
    attempts = _col(stage, "attempts")
    condition = and_(status == "pending", or_(next_at.is_(None), next_at <= moment))
    if extra_filter is not None:
        condition = and_(condition, extra_filter)
    rows = (
        await database.execute(
            select(stage.model.id, attempts)
            .where(condition)
            .order_by(next_at.asc().nulls_first(), stage.model.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not rows:
        await database.rollback()
        return []
    claims: list[Claim] = []
    for row_id, current in rows:
        token = uuid.uuid4()
        await database.execute(
            update(stage.model)
            .where(stage.model.id == row_id)
            .values(
                {
                    f"{stage.column}_attempts": current + 1,
                    f"{stage.column}_claim_token": token,
                    f"{stage.column}_next_attempt_at": moment + stage.lease,
                }
            )
        )
        claims.append(Claim(stage=stage, row_id=row_id, attempt=current + 1, token=token))
    await database.commit()
    return claims


def _fenced(claim_: Claim) -> Any:
    return and_(
        claim_.stage.model.id == claim_.row_id,
        _col(claim_.stage, "claim_token") == claim_.token,
        _col(claim_.stage, "status") == "pending",
    )


async def settle_success(
    database: AsyncSession, claim_: Claim, values: dict[str, Any] | None = None
) -> bool:
    """Mark the stage done and write the stage's result columns in one statement.

    ``values`` may override ``<stage>_status`` with another terminal state (for
    example ``needs_body``); it must never set it back to ``pending``.
    """
    stage = claim_.stage
    status_key = f"{stage.column}_status"
    if (values or {}).get(status_key) == "pending":
        raise ValueError("a stage handler cannot re-queue its own row")
    result = await database.execute(
        update(stage.model)
        .where(_fenced(claim_))
        .values(
            {
                status_key: stage.done_status,
                f"{stage.column}_claim_token": None,
                f"{stage.column}_next_attempt_at": None,
                f"{stage.column}_error_code": None,
                **(values or {}),
            }
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def settle_retryable(
    database: AsyncSession,
    claim_: Claim,
    error: RetryableStageError,
    *,
    now: datetime | None = None,
) -> str:
    """Schedule the next attempt, or fail the row once attempts are exhausted.

    An error that does not count an attempt hands the claimed attempt back and
    never fails the row. Without ``retry_after`` it waits ``MAX_BACKOFF``, so
    such waits cannot turn into a fast loop.
    """
    stage = claim_.stage
    moment = now or datetime.now(UTC)
    if not error.counts_attempt:
        result = await database.execute(
            update(stage.model)
            .where(_fenced(claim_))
            .values(
                {
                    f"{stage.column}_attempts": claim_.attempt - 1,
                    f"{stage.column}_claim_token": None,
                    f"{stage.column}_next_attempt_at": moment
                    + backoff(claim_.attempt, error.retry_after or MAX_BACKOFF),
                    f"{stage.column}_error_code": error.code[:100],
                }
            )
        )
        return "pending" if getattr(result, "rowcount", 0) else "fenced"
    exhausted = claim_.attempt >= stage.max_attempts
    result = await database.execute(
        update(stage.model)
        .where(_fenced(claim_))
        .values(
            {
                f"{stage.column}_status": "failed" if exhausted else "pending",
                f"{stage.column}_claim_token": None,
                f"{stage.column}_next_attempt_at": None
                if exhausted
                else moment + backoff(claim_.attempt, error.retry_after),
                f"{stage.column}_error_code": error.code[:100],
            }
        )
    )
    if not getattr(result, "rowcount", 0):
        return "fenced"
    return "failed" if exhausted else "pending"


async def settle_fatal(database: AsyncSession, claim_: Claim, error: FatalStageError) -> bool:
    stage = claim_.stage
    result = await database.execute(
        update(stage.model)
        .where(_fenced(claim_))
        .values(
            {
                f"{stage.column}_status": "failed",
                f"{stage.column}_claim_token": None,
                f"{stage.column}_next_attempt_at": None,
                f"{stage.column}_error_code": error.code[:100],
            }
        )
    )
    return bool(getattr(result, "rowcount", 0))


StageHandler = Callable[[AsyncSession, Claim], Awaitable[dict[str, Any] | None]]
"""Does the work for one claimed row inside an open session.

Returns the result columns to write with the success settle (or None). It may
also write other rows in the same session; everything commits together.
"""

FatalHook = Callable[[Claim, FatalStageError], Awaitable[None]]
WaitHook = Callable[[Claim, RetryableStageError], Awaitable[None]]


class _CallAudits:
    """Every model call audit a claim's session has seen, flushed or not.

    Each call is paid for whatever happens to the stage, so every rollback in
    ``run_claimed`` discards the product writes and then re-adds these rows.
    Autoflush may already have written a row before the stage failed; the
    listener keeps a reference that survives the rollback.
    """

    def __init__(self, database: AsyncSession) -> None:
        self._database = database
        self._flushed: list[NewsroomLlmCall] = []
        event.listen(database.sync_session, "pending_to_persistent", self._remember)

    def _remember(self, session: Session, row: object) -> None:
        if isinstance(row, NewsroomLlmCall):
            self._flushed.append(row)

    async def rollback(self, extra: Sequence[Any] = ()) -> None:
        rows = [
            *self._flushed,
            *(row for row in self._database.new if isinstance(row, NewsroomLlmCall)),
            *extra,
        ]
        await self._database.rollback()
        # A stage may both leave a row in the session and attach it to its error.
        self._flushed.clear()
        self._database.add_all(list({id(row): row for row in rows}.values()))

    def close(self) -> None:
        event.remove(self._database.sync_session, "pending_to_persistent", self._remember)


async def run_claimed(
    session_factory: async_sessionmaker[AsyncSession],
    claim_: Claim,
    handler: StageHandler,
    *,
    on_fatal: FatalHook | None = None,
    on_wait: WaitHook | None = None,
) -> str:
    """Run ``handler`` for one claim and settle the row. Returns the outcome.

    After the row is settled, ``on_fatal`` hears about fatal errors and
    ``on_wait`` about retryable errors that do not count an attempt.
    """
    async with session_factory() as database:
        audits = _CallAudits(database)
        try:
            values = await handler(database, claim_)
            settled = await settle_success(database, claim_, values)
            if not settled:
                await audits.rollback()
            await database.commit()
            return "done" if settled else "fenced"
        except RetryableStageError as error:
            await audits.rollback(error.audit_rows)
            outcome = await settle_retryable(database, claim_, error)
            await database.commit()
            if outcome != "fenced" and on_wait is not None and not error.counts_attempt:
                await on_wait(claim_, error)
            return outcome
        except FatalStageError as error:
            await audits.rollback(error.audit_rows)
            settled = await settle_fatal(database, claim_, error)
            await database.commit()
            if settled and on_fatal is not None and error.notify:
                await on_fatal(claim_, error)
            return "fatal" if settled else "fenced"
        except Exception:
            # A bug must not retry forever: it counts as a retryable attempt so
            # max_attempts eventually fails the row, and the worker logs it.
            await audits.rollback()
            await settle_retryable(database, claim_, RetryableStageError("unexpected_error"))
            await database.commit()
            raise
        finally:
            audits.close()


async def enqueue(
    database: AsyncSession,
    stage: StageSpec,
    row_ids: Sequence[uuid.UUID],
    *,
    reset_attempts: bool = True,
) -> None:
    """Put rows (back) on a stage's queue, e.g. "re-analyse" from the admin console."""
    if not row_ids:
        return
    await database.execute(
        update(stage.model)
        .where(stage.model.id.in_(list(row_ids)))
        .values(_reset(stage, "pending", reset_attempts=reset_attempts))
    )


def _reset(stage: StageSpec, status: str, *, reset_attempts: bool = True) -> dict[str, Any]:
    values: dict[str, Any] = {
        f"{stage.column}_status": status,
        f"{stage.column}_claim_token": None,
        f"{stage.column}_next_attempt_at": None,
        f"{stage.column}_error_code": None,
    }
    if reset_attempts:
        values[f"{stage.column}_attempts"] = 0
    return values


def failed_with(stage: StageSpec, error_codes: Sequence[str]) -> Any:
    """Rows that failed ``stage`` with one of ``error_codes``."""
    return and_(_col(stage, "status") == "failed", _col(stage, "error_code").in_(list(error_codes)))


def in_flight(stage: StageSpec, now: datetime) -> Any:
    """Rows a worker may be processing, or backing off from a retryable error.

    Recovery leaves them alone: resetting one fences off the running attempt
    after its model call has already been paid for.
    """
    return and_(_col(stage, "status") == "pending", _col(stage, "next_attempt_at") > now)


async def requeue_failed(
    database: AsyncSession, stage: StageSpec, row_ids: Sequence[uuid.UUID]
) -> int:
    """``enqueue`` the rows that are still ``failed``; returns how many it put back."""
    if not row_ids:
        return 0
    result = await database.execute(
        update(stage.model)
        .where(stage.model.id.in_(list(row_ids)), _col(stage, "status") == "failed")
        .values(_reset(stage, "pending"))
    )
    return int(getattr(result, "rowcount", 0))


async def park(database: AsyncSession, stage: StageSpec, row_ids: Sequence[uuid.UUID]) -> None:
    """Take rows off a stage's queue (``idle``) until an earlier stage queues them again.

    An article sent back to ``embed`` parks its triage: the new embedding
    queues triage on success, and nothing triages the old one meanwhile.
    """
    if not row_ids:
        return
    await database.execute(
        update(stage.model).where(stage.model.id.in_(list(row_ids))).values(_reset(stage, "idle"))
    )


FETCH = StageSpec("fetch", NewsroomArticle, "fetch", "done", max_attempts=4)
EMBED = StageSpec("embed", NewsroomArticle, "embed", "done", max_attempts=6)
TRIAGE = StageSpec("triage", NewsroomArticle, "triage", "done", max_attempts=6)
ANALYSIS = StageSpec("analysis", NewsroomEvent, "analysis", "ready", max_attempts=5)
WHY = StageSpec("why", NewsroomEditionItem, "why", "ready", max_attempts=5)
TRANSLATE = StageSpec("translate", NewsroomEvent, "en", "ready", max_attempts=6)

STAGES = (FETCH, EMBED, TRIAGE, ANALYSIS, WHY, TRANSLATE)
