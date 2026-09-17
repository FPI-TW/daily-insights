from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from typing import Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings, is_placeholder_value
from daily_insights_api.modules.news.api import (
    DERIVATION_VERSION,
    LOCALES,
    SUMMARY_PROMPT_VERSION,
    DeepSeekClient,
    LocalizedSummary,
    ModelCall,
    NewsCandidate,
    NewsCandidateBatch,
    NewsCandidatePublication,
    NewsEdition,
    NewsExecution,
    NewsGenerationAudit,
    NewsItem,
    NewsPresentation,
    PreparedNewsItem,
    Selection,
    _cap_discovery,
    _digest,
    _edition_status,
    _fetch_usable_candidates,
    _limit_candidates,
    _lock_key,
    discover_feed_candidates,
    edition_spec,
    effective_hostnames,
    feed_client,
    load_selection_criteria,
    news_execution,
    publish_candidates,
    publishable_selection,
)
from daily_insights_api.modules.orchestration.facts import fence_is_current
from daily_insights_api.modules.orchestration.models import FunctionAttempt, FunctionRun, JobRun
from daily_insights_api.modules.orchestration.worker import (
    AttemptStatus,
    ClaimedFunction,
    FunctionOutcome,
)

FUNCTION_MARKETS = {
    "news_global_refresh": "global",
    "news_tw_equity_refresh": "tw_equity",
    "news_us_equity_refresh": "us_equity",
}


def _refresh_status(prepared: int, summary_failures: int) -> tuple[AttemptStatus, str, bool]:
    if prepared and summary_failures:
        return "partial", "partial", True
    if prepared:
        return "succeeded", "ready", False
    return "unavailable", "unavailable", True


def build_news_handlers(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> dict[str, Any]:
    async def refresh(claimed: ClaimedFunction) -> FunctionOutcome:
        return await refresh_news(settings, session_factory, claimed)

    async def publish(claimed: ClaimedFunction) -> FunctionOutcome:
        return await publish_news(settings, session_factory, claimed)

    return {**{key: refresh for key in FUNCTION_MARKETS}, "news_publish": publish}


def _client(settings: Settings) -> DeepSeekClient | None:
    api_key = settings.news_model_api_key
    if api_key is None:
        return None
    raw = api_key.get_secret_value().strip()
    if not raw or is_placeholder_value(raw):
        return None
    return DeepSeekClient(
        base_url=settings.model_api_base_url,
        api_key=raw,
        model=settings.model_name,
        timeout_seconds=settings.model_timeout_seconds,
        selection_criteria=load_selection_criteria(),
    )


async def _cancel_candidate_batch(database: AsyncSession, batch_id: uuid.UUID) -> None:
    stored = await database.get(NewsCandidateBatch, batch_id, with_for_update=True)
    if stored is not None:
        stored.status = "cancelled"
        stored.finalized_at = datetime.now(UTC)


async def refresh_news(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
) -> FunctionOutcome:
    if not settings.daily_news_enabled:
        return FunctionOutcome(status="unavailable", error_code="daily_news_disabled")
    client = _client(settings)
    if client is None:
        return FunctionOutcome(status="unavailable", error_code="news_model_unavailable")
    market = FUNCTION_MARKETS[claimed.function_key]
    spec = edition_spec(market)
    allowed = effective_hostnames(settings.news_extra_hostnames, settings.news_blocked_hostnames)
    batch = NewsCandidateBatch(
        function_attempt_id=claimed.attempt_id,
        edition_date=(await _job_edition(session_factory, claimed.job_run_id)),
        market_code=market,
        status="collecting",
    )
    try:
        async with session_factory.begin() as database:
            if not await fence_is_current(
                database,
                function_run_id=claimed.function_run_id,
                fence_token=claimed.fence_token,
            ):
                await _cancel_candidate_batch(database, batch.id)
                return FunctionOutcome(status="cancelled", error_code="cancelled")
            database.add(batch)
        bodies: dict[str, str] = {}
        async with feed_client(settings.news_discovery_timeout_seconds) as feeds_http:
            discovered = await discover_feed_candidates(
                feeds_http, allowed, market=market, bodies=bodies
            )
        capped = _cap_discovery(
            discovered,
            per_source=spec.max_discovery_per_source,
            total=spec.max_discovery_total,
            full_text_ids=frozenset(bodies),
            interleave=spec.interleave_sources,
            impact_patterns=spec.headline_impact_patterns,
        )
        extracted = await _fetch_usable_candidates(
            capped, allowed, settings.news_fetch_timeout_seconds, bodies
        )
        usable = _limit_candidates(
            extracted,
            total=spec.max_candidates,
            per_source=spec.max_per_source,
            interleave=spec.interleave_sources,
            impact_patterns=spec.headline_impact_patterns,
        )
        digest = _digest(
            usable,
            client.model_name,
            client.selection_prompt_digest,
            market,
            spec.selection,
        )
        candidate_rows = {
            candidate.id: NewsCandidate(
                edition_id=None,
                batch_id=batch.id,
                candidate_id=candidate.id,
                source_name=candidate.source_name,
                hostname=candidate.hostname,
                url=str(candidate.url),
                headline=candidate.headline,
                seen_at=candidate.seen_at,
                source_published_at=None,
                stage="discovered",
            )
            for candidate in discovered
        }
        fetched_by_id = {item.candidate.id: item for item in usable}
        for item in usable:
            row = candidate_rows[item.candidate.id]
            row.stage = "reviewed"
            row.content_digest = item.content_digest
            row.source_published_at = item.source_published_at
        async with session_factory.begin() as database:
            if not await fence_is_current(
                database,
                function_run_id=claimed.function_run_id,
                fence_token=claimed.fence_token,
            ):
                await _cancel_candidate_batch(database, batch.id)
                return FunctionOutcome(status="cancelled", error_code="cancelled")
            database.add_all(candidate_rows.values())
            await database.flush()
        prepared = 0
        summary_failures = 0
        selection_call: ModelCall | None = None
        if usable:
            selection_call = await client.select(usable, policy=spec.selection, previous_events=())
            assert isinstance(selection_call.value, Selection)
            publication = publishable_selection(
                list(selection_call.value.selections), usable, spec.selection
            )
            for rank, selected in enumerate(publication.selections, start=1):
                fetched = fetched_by_id[selected.id]
                summaries: dict[str, LocalizedSummary] = {}
                summary_calls: list[tuple[str, ModelCall]] = []
                try:
                    for locale in LOCALES:
                        call = await client.summarize(fetched.candidate, fetched.body, locale)
                        assert isinstance(call.value, LocalizedSummary)
                        summaries[locale] = call.value
                        summary_calls.append((locale, call))
                except Exception:
                    summary_failures += 1
                    candidate_rows[selected.id].stage = "dropped"
                    candidate_rows[selected.id].drop_reason = "summary_failed"
                    async with session_factory.begin() as database:
                        if not await fence_is_current(
                            database,
                            function_run_id=claimed.function_run_id,
                            fence_token=claimed.fence_token,
                        ):
                            await _cancel_candidate_batch(database, batch.id)
                            return FunctionOutcome(status="cancelled", error_code="cancelled")
                        database.add(candidate_rows[selected.id])
                    continue
                row = candidate_rows[selected.id]
                row.ai_rank = rank
                row.ai_topic = selected.topic
                row.ai_market = selected.market
                row.ai_importance = selected.importance
                row.ai_event_key = selected.event_key
                async with session_factory.begin() as database:
                    if not await fence_is_current(
                        database,
                        function_run_id=claimed.function_run_id,
                        fence_token=claimed.fence_token,
                    ):
                        await _cancel_candidate_batch(database, batch.id)
                        return FunctionOutcome(status="cancelled", error_code="cancelled")
                    database.add(row)
                    database.add(
                        PreparedNewsItem(
                            batch_id=batch.id,
                            candidate_id=row.id,
                            rank=rank,
                            topic=selected.topic,
                            importance=selected.importance,
                            market=selected.market,
                            event_key=selected.event_key,
                            numeric_facts=list(summaries["en"].numeric_facts),
                            presentations={
                                locale: summary.model_dump(mode="json")
                                for locale, summary in summaries.items()
                            },
                            content_digest=fetched.content_digest,
                        )
                    )
                    database.add_all(
                        _audit(
                            batch.id,
                            "summary",
                            locale,
                            call,
                            client.model_name,
                            SUMMARY_PROMPT_VERSION,
                        )
                        for locale, call in summary_calls
                    )
                prepared += 1
        async with session_factory.begin() as database:
            if not await fence_is_current(
                database,
                function_run_id=claimed.function_run_id,
                fence_token=claimed.fence_token,
            ):
                await _cancel_candidate_batch(database, batch.id)
                return FunctionOutcome(status="cancelled", error_code="cancelled")
            stored = await database.get(NewsCandidateBatch, batch.id, with_for_update=True)
            assert stored is not None
            if selection_call is not None:
                database.add(
                    _audit(
                        batch.id,
                        "selection",
                        None,
                        selection_call,
                        client.model_name,
                        client.selection_prompt_version,
                    )
                )
            outcome_status, batch_status, retryable = _refresh_status(prepared, summary_failures)
            stored.status = batch_status
            stored.input_digest = digest
            stored.source_as_of = max(
                (item.source_published_at for item in usable if item.source_published_at),
                default=datetime.now(UTC),
            )
            stored.result = {
                "discovered": len(discovered),
                "usable": len(usable),
                "prepared": prepared,
                "summary_failed": summary_failures,
                "model_name": client.model_name,
                "prompt_version": (f"{client.selection_prompt_version}+{SUMMARY_PROMPT_VERSION}"),
            }
            stored.finalized_at = datetime.now(UTC)
        return FunctionOutcome(
            status=outcome_status,
            source_as_of=batch.edition_date,
            fetched_at=datetime.now(UTC),
            record_count=prepared,
            payload_digest=digest,
            result={
                "batch_id": str(batch.id),
                "market_code": market,
                "prepared": prepared,
                "summary_failed": summary_failures,
            },
            missing_scopes=(market,) if summary_failures else (),
            error_code="news_summary_partial" if summary_failures else None,
            retryable=retryable,
        )
    except Exception:
        async with session_factory.begin() as database:
            stored = await database.get(NewsCandidateBatch, batch.id, with_for_update=True)
            if stored is not None:
                stored.status = "failed"
                stored.finalized_at = datetime.now(UTC)
        raise
    finally:
        await client.aclose()


def _publication_digest(batch: NewsCandidateBatch | None, prepared: list[PreparedNewsItem]) -> str:
    source_digest = batch.input_digest if batch and batch.input_digest else "0" * 64
    if not prepared:
        return source_digest
    payload = {
        "source_digest": source_digest,
        "prepared": [
            {
                "rank": item.rank,
                "content_digest": item.content_digest,
                "topic": item.topic,
                "importance": item.importance,
                "market": item.market,
                "event_key": item.event_key,
                "numeric_facts": item.numeric_facts,
                "presentations": item.presentations,
            }
            for item in prepared
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def publish_news(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedFunction,
) -> FunctionOutcome:
    if claimed.scope.get("edition_id") and claimed.scope.get("candidate_ids"):
        client = _client(settings)
        if client is None:
            return FunctionOutcome(status="unavailable", error_code="news_model_unavailable")
        try:

            async def guard() -> bool:
                async with session_factory.begin() as database:
                    return await fence_is_current(
                        database,
                        function_run_id=claimed.function_run_id,
                        fence_token=claimed.fence_token,
                    )

            async def guard_transaction(database: AsyncSession) -> bool:
                return await fence_is_current(
                    database,
                    function_run_id=claimed.function_run_id,
                    fence_token=claimed.fence_token,
                )

            if not await guard():
                return FunctionOutcome(status="cancelled", error_code="cancelled")
            actor = await _job_requester(session_factory, claimed.job_run_id)
            with news_execution(
                NewsExecution(
                    session_factory,
                    root_run_id=claimed.job_run_id,
                    run_id=claimed.attempt_id,
                    edition_date=claimed.edition_date,
                    guard=guard,
                    guard_transaction=guard_transaction,
                )
            ):
                requested_ids = (
                    claimed.scope.get("missing_scopes") or claimed.scope["candidate_ids"]
                )
                outcome, result, error = await publish_candidates(
                    session_factory,
                    client,
                    run_id=claimed.job_run_id,
                    edition_id=uuid.UUID(str(claimed.scope["edition_id"])),
                    candidate_ids=[uuid.UUID(str(value)) for value in requested_ids],
                    actor_user_id=actor,
                    allowed_hostnames=effective_hostnames(
                        settings.news_extra_hostnames, settings.news_blocked_hostnames
                    ),
                    fetch_timeout_seconds=settings.news_fetch_timeout_seconds,
                )
            status: AttemptStatus = (
                "succeeded"
                if outcome == "succeeded"
                else "partial"
                if outcome == "partial"
                else "failed"
            )
            published_count = result.get("published", 0)
            candidate_results = result.get("candidates", {})
            retry_codes = {
                "unexpected_error",
                "fetch_failed",
                "summary_failed",
                "waiting_recovery",
            }
            retry_ids = (
                tuple(
                    candidate_id
                    for candidate_id, code in candidate_results.items()
                    if code in retry_codes
                )
                if isinstance(candidate_results, dict)
                else ()
            )
            return FunctionOutcome(
                status=status,
                fetched_at=datetime.now(UTC),
                record_count=published_count if isinstance(published_count, int) else 0,
                result=result,
                error_code=error,
                missing_scopes=retry_ids,
                retryable=bool(retry_ids),
            )
        finally:
            await client.aclose()
    edition_date = await _job_edition(session_factory, claimed.job_run_id)
    async with session_factory.begin() as database:
        if not await fence_is_current(
            database,
            function_run_id=claimed.function_run_id,
            fence_token=claimed.fence_token,
        ):
            return FunctionOutcome(status="cancelled", error_code="cancelled")
        publish_job = await database.get(JobRun, claimed.job_run_id)
        if publish_job is None:
            return FunctionOutcome(status="cancelled", error_code="cancelled")
        attempts = (
            await database.scalars(
                select(FunctionAttempt)
                .join(FunctionRun, FunctionRun.id == FunctionAttempt.function_run_id)
                .where(
                    FunctionRun.job_run_id == claimed.job_run_id,
                    FunctionRun.function_key.in_(tuple(FUNCTION_MARKETS)),
                    FunctionAttempt.status.in_(
                        ("succeeded", "partial", "no_change", "unavailable", "failed")
                    ),
                )
                .order_by(FunctionAttempt.attempt_number.desc())
            )
        ).all()
        latest_attempt: dict[str, FunctionAttempt] = {}
        for attempt in attempts:
            latest_attempt.setdefault(attempt.function_key, attempt)
        refresh_runs = list(
            await database.scalars(
                select(FunctionRun).where(
                    FunctionRun.job_run_id == claimed.job_run_id,
                    FunctionRun.function_key.in_(tuple(FUNCTION_MARKETS)),
                )
            )
        )
        preferred_batch_ids: dict[str, uuid.UUID] = {}
        for run in refresh_runs:
            batch_id = run.result.get("batch_id") if isinstance(run.result, dict) else None
            if isinstance(batch_id, str):
                preferred_batch_ids[run.function_key] = uuid.UUID(batch_id)
        latest_attempt_ids = tuple(attempt.id for attempt in latest_attempt.values())
        preferred_ids = tuple(preferred_batch_ids.values())
        batches = list(
            (
                await database.scalars(
                    select(NewsCandidateBatch).where(
                        or_(
                            NewsCandidateBatch.function_attempt_id.in_(latest_attempt_ids),
                            NewsCandidateBatch.id.in_(preferred_ids),
                        )
                    )
                )
            ).all()
        )
        published = 0
        unavailable: list[str] = []
        available: list[str] = []
        partial: list[str] = []
        actions: dict[str, str] = {}
        markets = tuple(
            market
            for function_key, market in FUNCTION_MARKETS.items()
            if any(run.function_key == function_key for run in refresh_runs)
        )
        for market in markets:
            function_key = next(
                key
                for key, function_market in FUNCTION_MARKETS.items()
                if function_market == market
            )
            preferred_id = preferred_batch_ids.get(function_key)
            fallback_attempt = latest_attempt.get(function_key)
            batch = next(
                (
                    item
                    for item in batches
                    if item.id == preferred_id
                    or (
                        preferred_id is None
                        and fallback_attempt is not None
                        and item.function_attempt_id == fallback_attempt.id
                    )
                ),
                None,
            )
            prepared = (
                list(
                    (
                        await database.scalars(
                            select(PreparedNewsItem)
                            .where(PreparedNewsItem.batch_id == batch.id)
                            .order_by(PreparedNewsItem.rank)
                        )
                    ).all()
                )
                if batch is not None
                else []
            )
            input_digest = _publication_digest(batch, prepared)
            await database.execute(
                select(func.pg_advisory_xact_lock(_lock_key(edition_date, market)))
            )
            latest = await database.scalar(
                select(NewsEdition)
                .where(
                    NewsEdition.edition_date == edition_date,
                    NewsEdition.market_code == market,
                )
                .order_by(NewsEdition.revision.desc())
                .limit(1)
                .with_for_update()
            )
            if latest is not None and latest.input_digest == input_digest:
                actions[market] = "no_change"
                if latest.status == "complete":
                    available.append(market)
                elif latest.status == "partial":
                    available.append(market)
                    partial.append(market)
                else:
                    unavailable.append(market)
                continue
            latest_batch = (
                await database.get(NewsCandidateBatch, latest.candidate_batch_id)
                if latest is not None and latest.candidate_batch_id is not None
                else None
            )
            latest_job = (
                await database.get(JobRun, latest.publication_job_run_id)
                if latest is not None and latest.publication_job_run_id is not None
                else None
            )
            superseded = latest is not None and (
                latest.publication_job_run_id is None
                or (
                    batch is not None
                    and latest_batch is not None
                    and latest_batch.created_at >= batch.created_at
                )
                or (
                    (batch is None or latest_batch is None)
                    and latest_job is not None
                    and latest_job.created_at >= publish_job.created_at
                )
            )
            if superseded:
                assert latest is not None
                actions[market] = "superseded"
                if latest.status == "complete":
                    available.append(market)
                elif latest.status == "partial":
                    available.append(market)
                    partial.append(market)
                else:
                    unavailable.append(market)
                continue
            edition_status, caveat = _edition_status(
                len(prepared), edition_spec(market).target_items
            )
            batch_result = batch.result if batch and batch.result else {}
            edition = NewsEdition(
                edition_date=edition_date,
                market_code=market,
                revision=1 if latest is None else latest.revision + 1,
                input_digest=input_digest,
                derivation_version=DERIVATION_VERSION,
                model_name=cast(str | None, batch_result.get("model_name")),
                prompt_version=cast(
                    str,
                    batch_result.get(
                        "prompt_version", f"orchestration.v1+{SUMMARY_PROMPT_VERSION}"
                    ),
                ),
                candidate_batch_id=batch.id if batch is not None else None,
                publication_job_run_id=publish_job.id,
                status=edition_status,
                caveat=caveat,
            )
            database.add(edition)
            await database.flush()
            for rank, prepared_item in enumerate(prepared, start=1):
                candidate = await database.get(NewsCandidate, prepared_item.candidate_id)
                assert candidate is not None
                item = NewsItem(
                    edition_id=edition.id,
                    rank=rank,
                    topic=prepared_item.topic,
                    source_name=candidate.source_name,
                    source_hostname=candidate.hostname,
                    source_url=candidate.url,
                    source_headline=candidate.headline,
                    source_published_at=candidate.source_published_at,
                    importance=prepared_item.importance,
                    content_digest=prepared_item.content_digest,
                    numeric_facts=prepared_item.numeric_facts,
                    market=prepared_item.market,
                    event_key=prepared_item.event_key,
                    origin="model",
                )
                database.add(item)
                await database.flush()
                database.add_all(
                    NewsPresentation(
                        item_id=item.id,
                        locale=locale,
                        headline=str(presentation["headline"]),
                        summary=str(presentation["summary"]),
                    )
                    for locale, presentation in prepared_item.presentations.items()
                )
                database.add(
                    NewsCandidatePublication(
                        publish_job_run_id=claimed.job_run_id,
                        candidate_id=candidate.id,
                        item_id=item.id,
                    )
                )
                candidate.stage = "published"
                candidate.item_id = item.id
                published += 1
            if edition_status == "unavailable":
                unavailable.append(market)
            elif edition_status == "partial":
                available.append(market)
                partial.append(market)
            else:
                available.append(market)
            actions[market] = "published"
    return FunctionOutcome(
        status="partial"
        if partial or (unavailable and available)
        else "unavailable"
        if unavailable
        else "succeeded",
        source_as_of=edition_date,
        fetched_at=datetime.now(UTC),
        record_count=published,
        result={
            "markets": actions,
            "available": available,
            "partial": partial,
            "unavailable": unavailable,
        },
        retryable=False,
    )


async def _job_edition(
    session_factory: async_sessionmaker[AsyncSession], job_run_id: uuid.UUID
) -> date:
    from daily_insights_api.modules.orchestration.models import JobRun

    async with session_factory() as database:
        job = await database.get(JobRun, job_run_id)
        if job is None:
            raise ValueError("job run not found")
        return job.edition_date


async def _job_requester(
    session_factory: async_sessionmaker[AsyncSession], job_run_id: uuid.UUID
) -> uuid.UUID | None:
    from daily_insights_api.modules.orchestration.models import JobRun

    async with session_factory() as database:
        job = await database.get(JobRun, job_run_id)
        if job is None:
            raise ValueError("job run not found")
        return job.requested_by_user_id


def _audit(
    batch_id: uuid.UUID,
    stage: str,
    locale: str | None,
    call: ModelCall,
    model: str,
    prompt_version: str,
) -> NewsGenerationAudit:
    return NewsGenerationAudit(
        edition_id=None,
        candidate_batch_id=batch_id,
        stage=stage,
        locale=locale,
        provider="deepseek",
        model=model,
        prompt_version=prompt_version,
        input_digest=call.input_digest,
        status="succeeded",
        provider_request_id=call.request_id,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        latency_ms=call.latency_ms,
    )
