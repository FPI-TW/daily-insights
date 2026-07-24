import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.operations.models import ReportPipelineRun, SourceRun
from daily_insights_api.modules.reports.contracts import PublicationBundle
from daily_insights_api.modules.reports.models import (
    PublicationSourceRun,
    ReportPublication,
)

_ERROR_SECRET = re.compile(
    r"(?i)(?<![a-z0-9_-])"
    r"(?P<key>authorization|api[_-]?key|token|secret|password)"
    r"[\"']?\s*[:=]\s*"
    r"(?:(?P<quote>[\"'])(?:bearer\s+)?[^\"']*(?P=quote)"
    r"|(?:bearer\s+)?[^\s,;}&]+)"
)
_ERROR_CODE = re.compile(r"[^a-z0-9_.-]+")


@dataclass(frozen=True)
class PipelineSpec:
    report_key: str
    market_code: str
    edition_date: date
    revision: int
    derivation_version: str
    content_schema_version: str


@dataclass(frozen=True)
class PublishResult:
    publication: ReportPublication | None
    created: bool
    error_code: str | None = None


@dataclass(frozen=True)
class Freshness:
    status: Literal["fresh", "stale"]
    stale_reason: Literal["source_too_old", "latest_refresh_failed"] | None
    source_as_of: date


@dataclass(frozen=True)
class LastKnownGood:
    publication: ReportPublication
    freshness: Freshness


class PipelineBusyError(RuntimeError):
    pass


class PipelineLeaseLostError(RuntimeError):
    pass


def pipeline_idempotency_key(spec: PipelineSpec) -> str:
    canonical = json.dumps(
        {
            "content_schema_version": spec.content_schema_version,
            "derivation_version": spec.derivation_version,
            "edition_date": spec.edition_date.isoformat(),
            "market_code": spec.market_code,
            "report_key": spec.report_key,
            "revision": spec.revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def sanitize_error_code(code: str) -> str:
    normalized = _ERROR_CODE.sub("_", code.strip().lower()).strip("_.-")
    return (normalized or "unknown_error")[:100]


def sanitize_error_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    redacted = _ERROR_SECRET.sub(lambda match: f"{match.group('key')}=[REDACTED]", detail)
    printable = "".join(character if character.isprintable() else " " for character in redacted)
    collapsed = " ".join(printable.split())
    return collapsed[:1_000] or None


def _require_active_lease(
    pipeline_run: ReportPipelineRun,
    *,
    lease_owner: str,
    lease_attempt: int,
    now: datetime,
) -> None:
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    lease_expires_at = pipeline_run.lease_expires_at
    if lease_expires_at is not None and lease_expires_at.tzinfo is None:
        lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
    if (
        pipeline_run.status != "running"
        or pipeline_run.lease_owner != lease_owner
        or pipeline_run.attempt_count != lease_attempt
        or lease_expires_at is None
        or lease_expires_at <= now
    ):
        raise PipelineLeaseLostError("pipeline lease is no longer active")


async def claim_pipeline_run(
    database: AsyncSession,
    spec: PipelineSpec,
    *,
    lease_owner: str,
    now: datetime,
    lease_for: timedelta,
) -> ReportPipelineRun:
    """Claim a logical daily run inside the caller's PostgreSQL transaction.

    Callers commit this claim before contacting a provider. A restart can claim
    the same row only after its lease expires; the natural key and deterministic
    idempotency key prevent duplicate logical work.
    """

    if spec.revision <= 0:
        raise ValueError("revision must be positive")
    if not lease_owner or len(lease_owner) > 100:
        raise ValueError("lease_owner must contain between 1 and 100 characters")
    if lease_for <= timedelta(0):
        raise ValueError("lease_for must be positive")
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")

    idempotency_key = pipeline_idempotency_key(spec)
    advisory_key = int(idempotency_key[:16], 16)
    if advisory_key >= 2**63:
        advisory_key -= 2**64
    await database.execute(select(func.pg_advisory_xact_lock(advisory_key)))
    await database.execute(
        insert(ReportPipelineRun)
        .values(
            report_key=spec.report_key,
            market_code=spec.market_code,
            edition_date=spec.edition_date,
            revision=spec.revision,
            derivation_version=spec.derivation_version,
            content_schema_version=spec.content_schema_version,
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_nothing(index_elements=[ReportPipelineRun.idempotency_key])
    )
    run = (
        await database.scalars(
            select(ReportPipelineRun)
            .where(ReportPipelineRun.idempotency_key == idempotency_key)
            .with_for_update()
        )
    ).one()
    if run.status == "published":
        return run
    if run.status == "running" and run.lease_expires_at is not None:
        lease_expires_at = run.lease_expires_at
        if lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
        if lease_expires_at > now:
            raise PipelineBusyError("pipeline run already has an active lease")

    run.status = "running"
    run.attempt_count += 1
    run.lease_owner = lease_owner
    run.lease_expires_at = now + lease_for
    run.started_at = run.started_at or now
    run.finished_at = None
    run.error_code = None
    run.error_detail = None
    await database.flush()
    return run


async def start_source_run(
    database: AsyncSession,
    *,
    pipeline_run_id: uuid.UUID,
    lease_owner: str,
    lease_attempt: int,
    provider: str,
    dataset_key: str,
    attempt: int,
    contract_version: str,
    contract_hash: str,
    endpoint: str,
    request_fingerprint: str,
    now: datetime,
) -> SourceRun:
    pipeline_run = (
        await database.scalars(
            select(ReportPipelineRun)
            .where(ReportPipelineRun.id == pipeline_run_id)
            .with_for_update()
        )
    ).one()
    _require_active_lease(
        pipeline_run,
        lease_owner=lease_owner,
        lease_attempt=lease_attempt,
        now=now,
    )
    if attempt <= 0:
        raise ValueError("attempt must be positive")
    if len(contract_hash) != 64:
        raise ValueError("contract_hash must be a SHA-256 hex digest")
    if not endpoint or len(endpoint) > 255:
        raise ValueError("endpoint must contain between 1 and 255 characters")
    if len(request_fingerprint) != 64:
        raise ValueError("request_fingerprint must be a SHA-256 hex digest")
    source_run = SourceRun(
        pipeline_run_id=pipeline_run_id,
        pipeline_attempt=lease_attempt,
        provider=provider,
        dataset_key=dataset_key,
        attempt=attempt,
        contract_version=contract_version,
        contract_hash=contract_hash,
        endpoint=endpoint,
        request_fingerprint=request_fingerprint,
        status="running",
        started_at=now,
    )
    database.add(source_run)
    await database.flush()
    return source_run


def complete_source_run(
    source_run: SourceRun,
    *,
    source_as_of: date,
    fetched_at: datetime,
    record_count: int,
    payload_sha256: str,
    provider_request_id: str | None,
    finished_at: datetime,
) -> None:
    if source_run.status != "running":
        raise ValueError("only a running source run can succeed")
    if fetched_at.tzinfo is None or finished_at.tzinfo is None:
        raise ValueError("fetch and finish timestamps must include a timezone")
    if record_count < 0:
        raise ValueError("record_count cannot be negative")
    if len(payload_sha256) != 64:
        raise ValueError("payload_sha256 must be a SHA-256 hex digest")
    source_run.status = "succeeded"
    source_run.source_as_of = source_as_of
    source_run.fetched_at = fetched_at
    source_run.finished_at = finished_at
    source_run.record_count = record_count
    source_run.payload_sha256 = payload_sha256
    source_run.provider_request_id = (
        provider_request_id[:255] if provider_request_id is not None else None
    )
    source_run.error_code = None
    source_run.error_detail = None


def fail_source_run(
    source_run: SourceRun,
    *,
    error_code: str,
    error_detail: str | None,
    finished_at: datetime,
) -> None:
    if source_run.status != "running":
        raise ValueError("only a running source run can fail")
    if finished_at.tzinfo is None:
        raise ValueError("finished_at must include a timezone")
    source_run.status = "failed"
    source_run.source_as_of = None
    source_run.fetched_at = None
    source_run.finished_at = finished_at
    source_run.record_count = None
    source_run.payload_sha256 = None
    source_run.error_code = sanitize_error_code(error_code)
    source_run.error_detail = sanitize_error_detail(error_detail)


async def publish_completed_run(
    database: AsyncSession,
    *,
    pipeline_run_id: uuid.UUID,
    lease_owner: str,
    lease_attempt: int,
    bundle: PublicationBundle,
    source_run_ids: tuple[uuid.UUID, ...],
    required_dataset_keys: frozenset[str],
    now: datetime,
) -> PublishResult:
    """Atomically publish complete normalized inputs inside the caller transaction."""

    pipeline_run = (
        await database.scalars(
            select(ReportPipelineRun)
            .where(ReportPipelineRun.id == pipeline_run_id)
            .with_for_update()
        )
    ).one()
    existing = (
        await database.scalars(
            select(ReportPublication).where(ReportPublication.pipeline_run_id == pipeline_run_id)
        )
    ).one_or_none()
    if existing is not None:
        return PublishResult(publication=existing, created=False)
    _require_active_lease(
        pipeline_run,
        lease_owner=lease_owner,
        lease_attempt=lease_attempt,
        now=now,
    )
    if bundle.content.market_code != pipeline_run.market_code:
        raise ValueError("publication market does not match pipeline market")
    if bundle.content.schema_version != pipeline_run.content_schema_version:
        raise ValueError("publication schema version does not match pipeline")

    unique_source_run_ids = set(source_run_ids)
    source_runs = (
        await database.scalars(
            select(SourceRun).where(
                SourceRun.id.in_(unique_source_run_ids),
                SourceRun.pipeline_run_id == pipeline_run_id,
                SourceRun.pipeline_attempt == lease_attempt,
                SourceRun.status == "succeeded",
            )
        )
    ).all()
    datasets = {source.dataset_key for source in source_runs}
    complete = (
        bool(required_dataset_keys)
        and bool(source_runs)
        and len(unique_source_run_ids) == len(source_run_ids)
        and len(source_runs) == len(source_run_ids)
        and required_dataset_keys <= datasets
        and all(
            source.record_count is not None and source.record_count > 0 for source in source_runs
        )
    )
    if not complete:
        pipeline_run.status = "failed"
        pipeline_run.finished_at = now
        pipeline_run.lease_owner = None
        pipeline_run.lease_expires_at = None
        pipeline_run.error_code = "incomplete_source_data"
        pipeline_run.error_detail = None
        await database.flush()
        return PublishResult(
            publication=None,
            created=False,
            error_code="incomplete_source_data",
        )

    source_as_of_values = [source.source_as_of for source in source_runs]
    assert all(value is not None for value in source_as_of_values)
    source_as_of = min(value for value in source_as_of_values if value is not None)
    if bundle.content.as_of != source_as_of:
        raise ValueError("publication as_of must equal the least-fresh required input")

    digest_material = "|".join(
        sorted(
            f"{source.provider}:{source.dataset_key}:{source.payload_sha256}"
            for source in source_runs
        )
    )
    input_digest = hashlib.sha256(
        f"{pipeline_run.derivation_version}|{digest_material}".encode()
    ).hexdigest()
    publication = ReportPublication(
        pipeline_run_id=pipeline_run.id,
        report_key=pipeline_run.report_key,
        market_code=pipeline_run.market_code,
        edition_date=pipeline_run.edition_date,
        revision=pipeline_run.revision,
        derivation_version=pipeline_run.derivation_version,
        content_schema_version=pipeline_run.content_schema_version,
        input_digest=input_digest,
        source_as_of=source_as_of,
        content=bundle.content_for_storage(),
        presentations=bundle.presentations_for_storage(),
        published_at=now,
        created_at=now,
    )
    database.add(publication)
    await database.flush()
    database.add_all(
        [
            PublicationSourceRun(publication_id=publication.id, source_run_id=source.id)
            for source in source_runs
        ]
    )
    pipeline_run.status = "published"
    pipeline_run.finished_at = now
    pipeline_run.lease_owner = None
    pipeline_run.lease_expires_at = None
    pipeline_run.error_code = None
    pipeline_run.error_detail = None
    await database.flush()
    return PublishResult(publication=publication, created=True)


def evaluate_freshness(
    publication: ReportPublication,
    *,
    now: datetime,
    maximum_age: timedelta,
    latest_failed_at: datetime | None = None,
) -> Freshness:
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    if maximum_age <= timedelta(0):
        raise ValueError("maximum_age must be positive")
    if latest_failed_at is not None:
        if latest_failed_at.tzinfo is None:
            raise ValueError("latest_failed_at must include a timezone")
        if latest_failed_at > publication.published_at:
            return Freshness(
                status="stale",
                stale_reason="latest_refresh_failed",
                source_as_of=publication.source_as_of,
            )
    if now.date() - publication.source_as_of > maximum_age:
        return Freshness(
            status="stale",
            stale_reason="source_too_old",
            source_as_of=publication.source_as_of,
        )
    return Freshness(status="fresh", stale_reason=None, source_as_of=publication.source_as_of)


async def get_last_known_good(
    database: AsyncSession,
    *,
    report_key: str,
    market_code: str,
    now: datetime,
    maximum_age: timedelta,
) -> LastKnownGood | None:
    publication = (
        await database.scalars(
            select(ReportPublication)
            .where(
                ReportPublication.report_key == report_key,
                ReportPublication.market_code == market_code,
            )
            .order_by(
                ReportPublication.edition_date.desc(),
                ReportPublication.revision.desc(),
                ReportPublication.published_at.desc(),
            )
            .limit(1)
        )
    ).one_or_none()
    if publication is None:
        return None
    latest_failed_at = (
        await database.scalars(
            select(ReportPipelineRun.finished_at)
            .where(
                ReportPipelineRun.report_key == report_key,
                ReportPipelineRun.market_code == market_code,
                ReportPipelineRun.status == "failed",
                ReportPipelineRun.finished_at.is_not(None),
                (
                    (ReportPipelineRun.edition_date > publication.edition_date)
                    | (
                        (ReportPipelineRun.edition_date == publication.edition_date)
                        & (ReportPipelineRun.revision > publication.revision)
                    )
                ),
            )
            .order_by(ReportPipelineRun.finished_at.desc())
            .limit(1)
        )
    ).one_or_none()
    return LastKnownGood(
        publication=publication,
        freshness=evaluate_freshness(
            publication,
            now=now,
            maximum_age=maximum_age,
            latest_failed_at=latest_failed_at,
        ),
    )
