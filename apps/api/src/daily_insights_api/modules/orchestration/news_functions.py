from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any, Literal, cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings, is_placeholder_value
from daily_insights_api.modules.news.api import (
    DERIVATION_VERSION,
    LOCALES,
    SUMMARY_PROMPT_VERSION,
    TRANSLATION_PROMPT_VERSION,
    DeepSeekClient,
    FetchedCandidate,
    LocalizedSummary,
    ModelCall,
    ModelCallError,
    NewsCandidate,
    NewsCandidateBatch,
    NewsCandidatePublication,
    NewsEdition,
    NewsExecution,
    NewsFailure,
    NewsGenerationAudit,
    NewsItem,
    NewsOperationError,
    NewsPresentation,
    PreparedNewsItem,
    Selection,
    _cap_discovery,
    _digest,
    _edition_status,
    _fetch_usable_candidates,
    _limit_candidates,
    _lock_key,
    classify_failure,
    discover_feed_candidates,
    edition_spec,
    effective_hostnames,
    feed_client,
    load_selection_criteria,
    news_execution,
    publish_candidates,
    publishable_selection,
)
from daily_insights_api.modules.operations.api import sanitize_error_code
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

ModelStage = Literal["selection", "summary", "translation"]
_MODEL_ATTEMPTS_KEY = "_news_model_attempts"


def _news_error_code(error: Exception) -> str:
    code = getattr(error, "error_code", None)
    return sanitize_error_code(code if isinstance(code, str) else type(error).__name__)


def _model_attempt_fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _failed_audit(
    batch_id: uuid.UUID,
    stage: ModelStage,
    locale: str | None,
    fallback_digest: str,
    model: str,
    prompt_version: str,
    error: Exception,
) -> NewsGenerationAudit:
    metadata = error if isinstance(error, ModelCallError) else None
    return NewsGenerationAudit(
        edition_id=None,
        candidate_batch_id=batch_id,
        stage=stage,
        locale=locale,
        provider="deepseek",
        model=model,
        prompt_version=prompt_version,
        input_digest=metadata.input_digest if metadata is not None else fallback_digest,
        status="failed",
        provider_request_id=metadata.request_id if metadata is not None else None,
        input_tokens=metadata.input_tokens if metadata is not None else None,
        output_tokens=metadata.output_tokens if metadata is not None else None,
        latency_ms=metadata.latency_ms if metadata is not None else 0,
        error_code=_news_error_code(error),
    )


async def _model_call_with_repair(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_id: uuid.UUID,
    stage: ModelStage,
    locale: str | None,
    fallback_digest: str,
    model: str,
    prompt_version: str,
    call: Callable[[str | None], Awaitable[ModelCall]],
    candidate_id: str | None = None,
    attempt_key: str | None = None,
) -> ModelCall:
    durable_key = (
        attempt_key
        or hashlib.sha256(
            json.dumps(
                [stage, locale, candidate_id, fallback_digest, model, prompt_version],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    for _ in range(2):
        attempt, feedback, previous_action, cached_call = await _reserve_model_attempt(
            session_factory, batch_id=batch_id, attempt_key=durable_key
        )
        if cached_call is not None:
            try:
                return _restore_model_call(cached_call, stage=stage)
            except (KeyError, TypeError, ValueError) as error:
                raise NewsOperationError(
                    NewsFailure(
                        code="model_checkpoint_invalid",
                        action="attention",
                        stage=stage,
                        candidate_id=candidate_id,
                        locale=locale,
                    )
                ) from error
        if attempt is None:
            if previous_action == "repair":
                failure = NewsFailure(
                    code=f"{feedback or f'{stage}_validation'}_exhausted",
                    action="skip" if stage in {"summary", "translation"} else "attention",
                    stage=stage,
                    candidate_id=candidate_id,
                    locale=locale,
                )
            else:
                failure = NewsFailure(
                    code=feedback or "model_attempt_outcome_unknown",
                    action=(
                        cast(Any, previous_action)
                        if previous_action in {"retry", "block", "attention"}
                        else "attention"
                    ),
                    stage=stage,
                    candidate_id=candidate_id,
                    locale=locale,
                )
            raise NewsOperationError(failure)
        try:
            model_call = await call(feedback)
        except Exception as error:
            failure = classify_failure(
                error,
                stage=stage,
                candidate_id=candidate_id,
                locale=locale,
            )
            async with session_factory.begin() as database:
                database.add(
                    _failed_audit(
                        batch_id,
                        stage,
                        locale,
                        fallback_digest,
                        model,
                        prompt_version,
                        error,
                    )
                )
                await _record_model_attempt_failure(
                    database,
                    batch_id=batch_id,
                    attempt_key=durable_key,
                    code=failure.code,
                    action=failure.action,
                )
            if attempt >= 2 or failure.action != "repair":
                if attempt >= 2 and failure.action == "repair":
                    failure = failure.model_copy(
                        update={
                            "action": "skip"
                            if stage in {"summary", "translation"}
                            else "attention",
                            "code": f"{failure.code}_exhausted",
                        }
                    )
                raise NewsOperationError(failure) from error
            continue
        await _record_model_attempt_success(
            session_factory,
            batch_id=batch_id,
            attempt_key=durable_key,
            model_call=model_call,
        )
        return model_call
    raise AssertionError("bounded news model repair loop exited unexpectedly")


async def _model_function_run(
    database: AsyncSession, batch_id: uuid.UUID, *, for_update: bool
) -> FunctionRun:
    function_run_id = await database.scalar(
        select(FunctionAttempt.function_run_id)
        .join(
            NewsCandidateBatch,
            NewsCandidateBatch.function_attempt_id == FunctionAttempt.id,
        )
        .where(NewsCandidateBatch.id == batch_id)
    )
    assert function_run_id is not None
    statement = select(FunctionRun).where(FunctionRun.id == function_run_id)
    if for_update:
        statement = statement.with_for_update()
    function_run = await database.scalar(statement)
    assert function_run is not None
    return function_run


async def _reserve_model_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_id: uuid.UUID,
    attempt_key: str,
) -> tuple[int | None, str | None, str | None, dict[str, Any] | None]:
    """Durably reserve a model dispatch so restarts cannot renew repair budget."""
    async with session_factory.begin() as database:
        function_run = await _model_function_run(database, batch_id, for_update=True)
        result = dict(function_run.result or {})
        attempts = dict(result.get(_MODEL_ATTEMPTS_KEY) or {})
        state = dict(attempts.get(attempt_key) or {})
        cached_call = state.get("success")
        if isinstance(cached_call, dict):
            return None, None, "success", cached_call
        count = state.get("count", 0)
        count = count if isinstance(count, int) and not isinstance(count, bool) else 0
        previous_code = state.get("failure_code")
        previous_code = previous_code if isinstance(previous_code, str) else None
        previous_action = state.get("failure_action")
        previous_action = previous_action if isinstance(previous_action, str) else None
        if count >= 2:
            if previous_action == "retry":
                previous_code = f"{previous_code or 'provider_retry'}_repair_exhausted"
                previous_action = "attention"
            return None, previous_code, previous_action, None
        if previous_action == "retry":
            count = 0
            previous_code = None
            previous_action = None
        elif previous_action in {"block", "attention"}:
            return None, previous_code, previous_action, None
        count += 1
        state["count"] = count
        attempts[attempt_key] = state
        result[_MODEL_ATTEMPTS_KEY] = attempts
        function_run.result = result
        return (
            count,
            previous_code if previous_action == "repair" else None,
            previous_action,
            None,
        )


def _serialize_model_call(model_call: ModelCall) -> dict[str, Any]:
    value_type = "selection" if isinstance(model_call.value, Selection) else "localized_summary"
    return {
        "value_type": value_type,
        "value": model_call.value.model_dump(mode="json"),
        "request_id": model_call.request_id,
        "input_tokens": model_call.input_tokens,
        "output_tokens": model_call.output_tokens,
        "latency_ms": model_call.latency_ms,
        "input_digest": model_call.input_digest,
    }


def _restore_model_call(payload: dict[str, Any], *, stage: ModelStage) -> ModelCall:
    value_type = payload["value_type"]
    value_payload = payload["value"]
    value: Selection | LocalizedSummary
    if value_type == "selection" and stage == "selection":
        value = Selection.model_validate(value_payload)
    elif value_type == "localized_summary" and stage in {"summary", "translation"}:
        value = LocalizedSummary.model_validate(value_payload)
    else:
        raise ValueError("model checkpoint stage mismatch")
    return ModelCall(
        value=value,
        request_id=payload.get("request_id"),
        input_tokens=payload.get("input_tokens"),
        output_tokens=payload.get("output_tokens"),
        latency_ms=int(payload["latency_ms"]),
        input_digest=str(payload["input_digest"]),
        reused=True,
    )


async def _record_model_attempt_success(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_id: uuid.UUID,
    attempt_key: str,
    model_call: ModelCall,
) -> None:
    async with session_factory.begin() as database:
        function_run = await _model_function_run(database, batch_id, for_update=True)
        result = dict(function_run.result or {})
        attempts = dict(result.get(_MODEL_ATTEMPTS_KEY) or {})
        state = dict(attempts.get(attempt_key) or {})
        state.pop("failure_code", None)
        state.pop("failure_action", None)
        state["success"] = _serialize_model_call(model_call)
        attempts[attempt_key] = state
        result[_MODEL_ATTEMPTS_KEY] = attempts
        function_run.result = result


async def _record_model_attempt_failure(
    database: AsyncSession,
    *,
    batch_id: uuid.UUID,
    attempt_key: str,
    code: str,
    action: str,
) -> None:
    function_run = await _model_function_run(database, batch_id, for_update=True)
    result = dict(function_run.result or {})
    attempts = dict(result.get(_MODEL_ATTEMPTS_KEY) or {})
    state = dict(attempts.get(attempt_key) or {})
    state.update(failure_code=sanitize_error_code(code), failure_action=action)
    attempts[attempt_key] = state
    result[_MODEL_ATTEMPTS_KEY] = attempts
    function_run.result = result


async def _persist_successful_audit(
    session_factory: async_sessionmaker[AsyncSession],
    audit: NewsGenerationAudit,
) -> None:
    async with session_factory.begin() as database:
        database.add(audit)


def _summary_call(
    client: DeepSeekClient,
    fetched: FetchedCandidate,
    locale: str,
) -> Callable[[str | None], Awaitable[ModelCall]]:
    async def call(feedback: str | None) -> ModelCall:
        return await client.summarize(
            fetched.candidate,
            fetched.body,
            locale,
            retry_feedback=feedback,
        )

    return call


def _translation_call(
    client: DeepSeekClient,
    fetched: FetchedCandidate,
    source_summary: LocalizedSummary,
    locale: str,
) -> Callable[[str | None], Awaitable[ModelCall]]:
    async def call(feedback: str | None) -> ModelCall:
        return await client.translate(
            fetched.candidate,
            fetched.body,
            source_summary,
            locale,
            retry_feedback=feedback,
        )

    return call


def _refresh_status(prepared: int, summary_failures: int) -> tuple[AttemptStatus, str, bool]:
    if prepared and summary_failures:
        return "partial", "partial", False
    if prepared:
        return "succeeded", "ready", False
    if summary_failures:
        return "unavailable", "unavailable", False
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
        raise NewsOperationError(
            NewsFailure(
                code="news_model_configuration_missing",
                action="block",
                stage="selection",
                scope="provider:news",
            )
        )
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
        summary_failure_reasons: dict[str, dict[str, str]] = {}
        selection_call: ModelCall | None = None
        if usable:
            selection_call = await _model_call_with_repair(
                session_factory,
                batch_id=batch.id,
                stage="selection",
                locale=None,
                fallback_digest=digest,
                model=client.model_name,
                prompt_version=client.selection_prompt_version,
                call=lambda feedback: client.select(
                    usable,
                    policy=spec.selection,
                    previous_events=(),
                    retry_feedback=feedback,
                ),
                attempt_key=_model_attempt_fingerprint(
                    [
                        "selection",
                        digest,
                        client.model_name,
                        client.selection_prompt_version,
                    ]
                ),
            )
            assert isinstance(selection_call.value, Selection)
            if not selection_call.reused:
                await _persist_successful_audit(
                    session_factory,
                    _audit(
                        batch.id,
                        "selection",
                        None,
                        selection_call,
                        client.model_name,
                        client.selection_prompt_version,
                    ),
                )
            publication = publishable_selection(
                list(selection_call.value.selections), usable, spec.selection
            )
            for rank, selected in enumerate(publication.selections, start=1):
                fetched = fetched_by_id[selected.id]
                summaries: dict[str, LocalizedSummary] = {}
                try:
                    locale = "zh-hant"
                    summary_call = await _model_call_with_repair(
                        session_factory,
                        batch_id=batch.id,
                        stage="summary",
                        locale=locale,
                        fallback_digest=hashlib.sha256(fetched.content_digest.encode()).hexdigest(),
                        model=client.model_name,
                        prompt_version=SUMMARY_PROMPT_VERSION,
                        call=_summary_call(client, fetched, locale),
                        candidate_id=selected.id,
                        attempt_key=_model_attempt_fingerprint(
                            [
                                "summary",
                                fetched.candidate.model_dump(mode="json"),
                                fetched.content_digest,
                                locale,
                                client.model_name,
                                SUMMARY_PROMPT_VERSION,
                            ]
                        ),
                    )
                    assert isinstance(summary_call.value, LocalizedSummary)
                    summaries[locale] = summary_call.value
                    if not summary_call.reused:
                        await _persist_successful_audit(
                            session_factory,
                            _audit(
                                batch.id,
                                "summary",
                                locale,
                                summary_call,
                                client.model_name,
                                SUMMARY_PROMPT_VERSION,
                            ),
                        )
                    source_summary = summary_call.value
                    for locale in LOCALES:
                        if locale == "zh-hant":
                            continue
                        translation = await _model_call_with_repair(
                            session_factory,
                            batch_id=batch.id,
                            stage="translation",
                            locale=locale,
                            fallback_digest=hashlib.sha256(
                                fetched.content_digest.encode()
                            ).hexdigest(),
                            model=client.model_name,
                            prompt_version=TRANSLATION_PROMPT_VERSION,
                            call=_translation_call(client, fetched, source_summary, locale),
                            candidate_id=selected.id,
                            attempt_key=_model_attempt_fingerprint(
                                [
                                    "translation",
                                    fetched.candidate.model_dump(mode="json"),
                                    fetched.content_digest,
                                    source_summary.model_dump(mode="json"),
                                    locale,
                                    client.model_name,
                                    SUMMARY_PROMPT_VERSION,
                                    TRANSLATION_PROMPT_VERSION,
                                ]
                            ),
                        )
                        assert isinstance(translation.value, LocalizedSummary)
                        summaries[locale] = translation.value
                        if not translation.reused:
                            await _persist_successful_audit(
                                session_factory,
                                _audit(
                                    batch.id,
                                    "translation",
                                    locale,
                                    translation,
                                    client.model_name,
                                    TRANSLATION_PROMPT_VERSION,
                                ),
                            )
                except NewsOperationError as error:
                    if not (
                        error.failure.action == "skip"
                        and error.failure.stage in {"summary", "translation"}
                    ):
                        raise
                    summary_failures += 1
                    summary_failure_reasons[selected.id] = {
                        "stage": error.failure.stage,
                        "locale": error.failure.locale or locale,
                        "code": sanitize_error_code(error.failure.code),
                    }
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
                "failure_reasons": summary_failure_reasons,
                "model_name": client.model_name,
                "prompt_version": (
                    f"{client.selection_prompt_version}+{SUMMARY_PROMPT_VERSION}"
                    f"+{TRANSLATION_PROMPT_VERSION}"
                ),
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
                "failure_reasons": summary_failure_reasons,
            },
            missing_scopes=(market,) if summary_failures else (),
            error_code="news_summary_partial" if summary_failures else None,
            error_detail=(
                "; ".join(
                    f"{candidate_id}={reason['stage']}:{reason['locale']}:{reason['code']}"
                    for candidate_id, reason in summary_failure_reasons.items()
                )[:500]
                or None
            ),
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
            raise NewsOperationError(
                NewsFailure(
                    code="news_model_configuration_missing",
                    action="block",
                    stage="summary",
                    scope="provider:news",
                )
            )
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
            retry_codes = {"waiting_recovery"}
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
        batches_by_id = {batch.id: batch for batch in batches}

        def refresh_batch(function_key: str) -> NewsCandidateBatch | None:
            preferred_id = preferred_batch_ids.get(function_key)
            if preferred_id is not None:
                return batches_by_id.get(preferred_id)
            attempt = latest_attempt.get(function_key)
            return next(
                (
                    batch
                    for batch in batches
                    if attempt is not None and batch.function_attempt_id == attempt.id
                ),
                None,
            )

        def unavailable_is_editorial(function_key: str) -> bool:
            """Only a completed zero-content refresh may publish an empty edition."""
            attempt = latest_attempt.get(function_key)
            batch = refresh_batch(function_key)
            return (
                attempt is not None
                and attempt.status == "unavailable"
                and batch is not None
                and batch.status == "unavailable"
            )

        published = 0
        unavailable: list[str] = []
        available: list[str] = []
        partial: list[str] = []
        blocked: list[str] = []
        actions: dict[str, str] = {}
        refresh_by_key = {run.function_key: run for run in refresh_runs}
        publishable_statuses = {"succeeded", "no_change", "partial"}
        publishable_markets: list[str] = []
        for function_key, market in FUNCTION_MARKETS.items():
            refresh_run = refresh_by_key.get(function_key)
            if refresh_run is None:
                continue
            if refresh_run.status in publishable_statuses or (
                refresh_run.status == "unavailable" and unavailable_is_editorial(function_key)
            ):
                publishable_markets.append(market)
            else:
                blocked.append(market)
                actions[market] = "skipped" if refresh_run.status == "cancelled" else "blocked"
        markets = tuple(publishable_markets)
        for market in markets:
            function_key = next(
                key
                for key, function_market in FUNCTION_MARKETS.items()
                if function_market == market
            )
            batch = refresh_batch(function_key)
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
    outcome_status: AttemptStatus = (
        "partial"
        if blocked and (available or unavailable)
        else "failed"
        if blocked
        else "partial"
        if partial or (unavailable and available)
        else "unavailable"
        if unavailable
        else "succeeded"
    )
    publication_result: dict[str, Any] = {
        "markets": actions,
        "available": available,
        "partial": partial,
        "unavailable": unavailable,
    }
    if blocked:
        publication_result["blocked"] = blocked
    return FunctionOutcome(
        status=outcome_status,
        source_as_of=edition_date,
        fetched_at=datetime.now(UTC),
        record_count=published,
        result=publication_result,
        error_code="news_refresh_blocked" if blocked else None,
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
