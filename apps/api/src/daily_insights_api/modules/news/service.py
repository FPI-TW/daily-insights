import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.news.contracts import Candidate, LocalizedSummary, Selection
from daily_insights_api.modules.news.llm import DeepSeekClient, ModelCall, ModelCallError
from daily_insights_api.modules.news.models import (
    NewsEdition,
    NewsGenerationAudit,
    NewsItem,
    NewsPresentation,
)
from daily_insights_api.modules.news.sources import (
    FetchedCandidate,
    configured_hostnames,
    discover_candidates,
    fetch_article,
    safe_article_client,
)

DERIVATION_VERSION = "gdelt-deepseek-news.v3"
SUMMARY_PROMPT_VERSION = "summary-v2"
LOCALES = ("zh-hant", "zh-hans", "en")
TAIPEI = ZoneInfo("Asia/Taipei")


async def _retry[T](
    call: Callable[[], Awaitable[T]], on_failure: Callable[[Exception], None] | None = None
) -> T:
    for attempt in range(2):
        try:
            return await call()
        except Exception as error:
            if on_failure is not None:
                on_failure(error)
            if attempt:
                raise
    raise AssertionError("unreachable")


def _digest(
    candidates: list[FetchedCandidate], model_name: str, selection_prompt_digest: str
) -> str:
    payload = {
        "derivation": DERIVATION_VERSION,
        "selection_prompt_digest": selection_prompt_digest,
        "summary_prompt": SUMMARY_PROMPT_VERSION,
        "model": model_name,
        "candidates": [
            {
                "candidate": fetched.candidate.model_dump(mode="json"),
                "source_url": fetched.source_url,
                "content_digest": fetched.content_digest,
            }
            for fetched in candidates
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _lock_key(edition_date: date) -> int:
    return int.from_bytes(
        hashlib.sha256(f"daily-news:{edition_date}".encode()).digest()[:8], "big", signed=True
    )


def _audit(
    edition_id: uuid.UUID,
    stage: str,
    locale: str | None,
    call: ModelCall,
    model: str,
    prompt_version: str,
) -> NewsGenerationAudit:
    return NewsGenerationAudit(
        edition_id=edition_id,
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


def _failed_audit(
    edition_id: uuid.UUID,
    stage: str,
    locale: str | None,
    input_digest: str,
    model: str,
    error: Exception,
    *,
    prompt_version: str = SUMMARY_PROMPT_VERSION,
) -> NewsGenerationAudit:
    metadata = error if isinstance(error, ModelCallError) else None
    return NewsGenerationAudit(
        edition_id=edition_id,
        stage=stage,
        locale=locale,
        provider="deepseek",
        model=model,
        prompt_version=prompt_version,
        input_digest=metadata.input_digest if metadata is not None else input_digest,
        status="failed",
        provider_request_id=metadata.request_id if metadata is not None else None,
        input_tokens=metadata.input_tokens if metadata is not None else None,
        output_tokens=metadata.output_tokens if metadata is not None else None,
        latency_ms=metadata.latency_ms if metadata is not None else 0,
        error_code=type(error).__name__,
    )


def _edition_status(count: int) -> tuple[str, str]:
    status = "complete" if count == 5 else "partial" if count else "unavailable"
    return status, f"{count}/5 stories completed"


async def _fetch_usable_candidates(
    candidates: list[Candidate], allowed: frozenset[str], timeout_seconds: float = 25
) -> list[FetchedCandidate]:
    semaphore = asyncio.Semaphore(6)
    async with safe_article_client(allowed, timeout_seconds) as http:

        async def fetch_one(candidate: Candidate) -> FetchedCandidate | None:
            async with semaphore:
                try:
                    source_url, body, source_published_at = await fetch_article(
                        http, str(candidate.url), allowed
                    )
                    emit_event("news.source.fetched", hostname=candidate.hostname, bytes=len(body))
                    return FetchedCandidate(
                        candidate,
                        source_url,
                        body,
                        hashlib.sha256(body.encode()).hexdigest(),
                        source_published_at,
                    )
                except Exception as error:
                    emit_event(
                        "news.source.failed",
                        hostname=candidate.hostname,
                        error_code=type(error).__name__,
                    )
                    return None

        fetched = await asyncio.gather(*(fetch_one(candidate) for candidate in candidates))
    return [item for item in fetched if item is not None]


async def run_news_edition(
    session_factory: async_sessionmaker[AsyncSession],
    client: DeepSeekClient,
    edition_date: date,
    *,
    allowed_hostnames: str,
    fetch_timeout_seconds: float = 25,
) -> None:
    """Discover, safely extract, then select and persist today's immutable edition."""
    if edition_date != datetime.now(TAIPEI).date():
        raise ValueError("daily news only generates the current Taipei edition")
    allowed = configured_hostnames(allowed_hostnames)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20), follow_redirects=False, cookies=None, trust_env=False
        ) as http:
            candidates = await discover_candidates(http, allowed)
    except Exception as error:
        emit_event("news.candidates.failed", error_code=type(error).__name__)
        candidates = []
    emit_event("news.candidates.discovered", count=len(candidates))
    usable = (
        await _fetch_usable_candidates(candidates, allowed, fetch_timeout_seconds)
        if candidates
        else []
    )
    usable = sorted(usable, key=lambda fetched: str(fetched.candidate.url))[:20]
    emit_event("news.sources.usable", count=len(usable))
    model_name = client.model_name
    selection_prompt_digest = client.selection_prompt_digest
    selection_prompt_version = client.selection_prompt_version
    edition_prompt_version = f"{selection_prompt_version}+{SUMMARY_PROMPT_VERSION}"
    input_digest = _digest(usable, model_name, selection_prompt_digest)
    async with session_factory() as database:
        await database.execute(select(func.pg_advisory_xact_lock(_lock_key(edition_date))))
        latest = (
            await database.scalars(
                select(NewsEdition)
                .where(NewsEdition.edition_date == edition_date)
                .order_by(NewsEdition.revision.desc())
                .limit(1)
                .with_for_update()
            )
        ).first()
        if latest is not None and latest.input_digest == input_digest:
            await database.rollback()
            emit_event("news.edition.idempotent", edition_date=edition_date)
            return
        edition = NewsEdition(
            edition_date=edition_date,
            revision=(latest.revision + 1 if latest else 1),
            input_digest=input_digest,
            derivation_version=DERIVATION_VERSION,
            model_name=model_name,
            prompt_version=edition_prompt_version,
            status="unavailable",
            caveat="0/5 stories completed",
        )
        database.add(edition)
        await database.flush()
        if not usable:
            await database.commit()
            emit_event("news.shortfall", status="unavailable", count=0)
            return
        try:
            selection_call = await _retry(
                lambda: client.select(usable),
                lambda error: database.add(
                    _failed_audit(
                        edition.id,
                        "selection",
                        None,
                        input_digest,
                        model_name,
                        error,
                        prompt_version=selection_prompt_version,
                    )
                ),
            )
            assert isinstance(selection_call.value, Selection)
            database.add(
                _audit(
                    edition.id,
                    "selection",
                    None,
                    selection_call,
                    model_name,
                    selection_prompt_version,
                )
            )
        except Exception as error:
            await database.commit()
            emit_event("news.selection.failed", error_code=type(error).__name__)
            return
        selected = {fetched.candidate.id: fetched for fetched in usable}
        complete_count = 0
        for selected_item in selection_call.value.selections:
            fetched = selected[selected_item.id]
            summaries: dict[str, LocalizedSummary] = {}
            try:
                for locale in LOCALES:

                    async def summarize_once(
                        candidate: Candidate = fetched.candidate,
                        body: str = fetched.body,
                        locale: str = locale,
                    ) -> ModelCall:
                        return await client.summarize(candidate, body, locale)

                    def audit_attempt_failure(
                        error: Exception,
                        locale: str = locale,
                        content_digest: str = fetched.content_digest,
                    ) -> None:
                        database.add(
                            _failed_audit(
                                edition.id,
                                "summary",
                                locale,
                                hashlib.sha256(content_digest.encode()).hexdigest(),
                                model_name,
                                error,
                                prompt_version=SUMMARY_PROMPT_VERSION,
                            )
                        )

                    call = await _retry(summarize_once, audit_attempt_failure)
                    assert isinstance(call.value, LocalizedSummary)
                    summaries[locale] = call.value
                    database.add(
                        _audit(
                            edition.id,
                            "summary",
                            locale,
                            call,
                            model_name,
                            SUMMARY_PROMPT_VERSION,
                        )
                    )
                item = NewsItem(
                    edition_id=edition.id,
                    rank=complete_count + 1,
                    topic=selected_item.topic,
                    source_name=fetched.candidate.source_name,
                    source_hostname=fetched.candidate.hostname,
                    source_url=fetched.source_url,
                    source_headline=fetched.candidate.headline,
                    source_published_at=fetched.source_published_at,
                    importance=selected_item.importance,
                    content_digest=fetched.content_digest,
                    numeric_facts=list(summaries["en"].numeric_facts),
                )
                database.add(item)
                await database.flush()
                for locale, summary in summaries.items():
                    database.add(
                        NewsPresentation(
                            item_id=item.id,
                            locale=locale,
                            headline=summary.headline,
                            summary=summary.summary,
                        )
                    )
                complete_count += 1
                emit_event("news.summary.succeeded", locale_count=3, rank=complete_count)
            except Exception as error:
                emit_event(
                    "news.summary.failed",
                    hostname=fetched.candidate.hostname,
                    error_code=type(error).__name__,
                )
        edition.status, edition.caveat = _edition_status(complete_count)
        await database.commit()
        emit_event("news.shortfall", status=edition.status, count=complete_count)
