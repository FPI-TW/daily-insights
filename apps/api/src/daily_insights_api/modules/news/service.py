import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.news.contracts import Candidate, LocalizedSummary, Selection
from daily_insights_api.modules.news.editions import (
    EDITION_ORDER,
    GLOBAL_SPEC,
    EditionSpec,
    edition_spec,
)
from daily_insights_api.modules.news.extraction import (
    FetchedCandidate,
    configured_hostnames,
    fetch_article,
    safe_article_client,
)
from daily_insights_api.modules.news.feeds import discover_feed_candidates, feed_client
from daily_insights_api.modules.news.llm import DeepSeekClient, ModelCall, ModelCallError
from daily_insights_api.modules.news.models import (
    NewsEdition,
    NewsGenerationAudit,
    NewsItem,
    NewsPresentation,
)

DERIVATION_VERSION = "feeds-deepseek-news.v4"
SUMMARY_PROMPT_VERSION = "summary-v2"
LOCALES = ("zh-hant", "zh-hans", "en")
TAIPEI = ZoneInfo("Asia/Taipei")
# Only a complete edition is final; partial and unavailable editions may be
# regenerated from identical inputs so a transient provider failure cannot
# freeze the day's news.
IDEMPOTENT_STATUS = "complete"
MAX_CANDIDATES = 20
MAX_CANDIDATES_PER_SOURCE = 5
# Extraction is the expensive stage, so discovery is capped per source before
# any article is fetched; the post-extraction cap above then selects the prompt.
MAX_DISCOVERY_PER_SOURCE = 10


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
    candidates: list[FetchedCandidate],
    model_name: str,
    selection_prompt_digest: str,
    market_code: str = GLOBAL_SPEC.market_code,
) -> str:
    payload = {
        "derivation": DERIVATION_VERSION,
        "market": market_code,
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


def _lock_key(edition_date: date, market_code: str = GLOBAL_SPEC.market_code) -> int:
    return int.from_bytes(
        hashlib.sha256(f"daily-news:{market_code}:{edition_date}".encode()).digest()[:8],
        "big",
        signed=True,
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


def _cap_discovery(
    candidates: list[Candidate], *, per_source: int = MAX_DISCOVERY_PER_SOURCE
) -> list[Candidate]:
    """Bound the number of articles fetched per source, newest first."""
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            candidate.seen_at is None,
            -(candidate.seen_at.timestamp() if candidate.seen_at else 0.0),
            str(candidate.url),
        ),
    )
    per_host: dict[str, int] = {}
    capped: list[Candidate] = []
    for candidate in ordered:
        if per_host.get(candidate.hostname, 0) >= per_source:
            continue
        per_host[candidate.hostname] = per_host.get(candidate.hostname, 0) + 1
        capped.append(candidate)
    return capped


def _limit_candidates(
    usable: list[FetchedCandidate],
    *,
    total: int = MAX_CANDIDATES,
    per_source: int = MAX_CANDIDATES_PER_SOURCE,
) -> list[FetchedCandidate]:
    """Keep the freshest candidates while bounding any single source.

    Ordering is newest first using the extracted publish time, falling back to
    the discovery ``seen_at`` (unknown timestamps last), with the source URL as
    a deterministic tie-breaker, so the selection prompt and ``input_digest``
    are reproducible for identical discovery results.
    """

    def sort_key(fetched: FetchedCandidate) -> tuple[bool, float, str]:
        freshness = fetched.source_published_at or fetched.candidate.seen_at
        return (
            freshness is None,
            -(freshness.timestamp() if freshness is not None else 0.0),
            fetched.source_url,
        )

    ordered = sorted(usable, key=sort_key)
    per_host: dict[str, int] = {}
    limited: list[FetchedCandidate] = []
    for fetched in ordered:
        host = fetched.candidate.hostname
        if per_host.get(host, 0) >= per_source:
            continue
        per_host[host] = per_host.get(host, 0) + 1
        limited.append(fetched)
        if len(limited) == total:
            break
    return limited


def _edition_status(count: int, target: int = GLOBAL_SPEC.target_items) -> tuple[str, str]:
    status = "complete" if count >= target else "partial" if count else "unavailable"
    return status, f"{count}/{target} stories completed"


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
    discovery_timeout_seconds: float = 30,
    spec: EditionSpec = GLOBAL_SPEC,
) -> str:
    """Discover, safely extract, then select and persist today's immutable edition.

    Returns the persisted edition status (``complete``, ``partial`` or
    ``unavailable``), or ``idempotent`` when a complete edition already exists
    for the same inputs.
    """
    if edition_date != datetime.now(TAIPEI).date():
        raise ValueError("daily news only generates the current Taipei edition")
    allowed = configured_hostnames(allowed_hostnames)
    market_code = spec.market_code
    async with feed_client(discovery_timeout_seconds) as feeds_http:
        feed_candidates = await discover_feed_candidates(feeds_http, allowed, market=market_code)
    emit_event("news.candidates.discovered", market=market_code, count=len(feed_candidates))
    # With a single discovery path, a registry-wide outage would otherwise
    # produce a quietly thin edition; the floor makes it visible early.
    floor = spec.target_items * 2
    if len(feed_candidates) < floor:
        emit_event(
            "news.candidates.below_floor",
            market=market_code,
            count=len(feed_candidates),
            floor=floor,
        )
    candidates = _cap_discovery(feed_candidates, per_source=spec.max_discovery_per_source)
    emit_event("news.candidates.merged", market=market_code, total=len(candidates))
    usable = (
        await _fetch_usable_candidates(candidates, allowed, fetch_timeout_seconds)
        if candidates
        else []
    )
    usable = _limit_candidates(usable, total=spec.max_candidates, per_source=spec.max_per_source)
    emit_event("news.sources.usable", market=market_code, count=len(usable))
    model_name = client.model_name
    selection_prompt_digest = client.selection_prompt_digest
    selection_prompt_version = client.selection_prompt_version
    edition_prompt_version = f"{selection_prompt_version}+{SUMMARY_PROMPT_VERSION}"
    input_digest = _digest(usable, model_name, selection_prompt_digest, market_code)
    async with session_factory() as database:
        await database.execute(
            select(func.pg_advisory_xact_lock(_lock_key(edition_date, market_code)))
        )
        latest = (
            await database.scalars(
                select(NewsEdition)
                .where(
                    NewsEdition.edition_date == edition_date,
                    NewsEdition.market_code == market_code,
                )
                .order_by(NewsEdition.revision.desc())
                .limit(1)
                .with_for_update()
            )
        ).first()
        if (
            latest is not None
            and latest.status == IDEMPOTENT_STATUS
            and latest.input_digest == input_digest
        ):
            await database.rollback()
            emit_event("news.edition.idempotent", edition_date=edition_date, market=market_code)
            return "idempotent"
        edition = NewsEdition(
            edition_date=edition_date,
            market_code=market_code,
            revision=(latest.revision + 1 if latest else 1),
            input_digest=input_digest,
            derivation_version=DERIVATION_VERSION,
            model_name=model_name,
            prompt_version=edition_prompt_version,
            status="unavailable",
            caveat=_edition_status(0, spec.target_items)[1],
        )
        database.add(edition)
        await database.flush()
        if not usable:
            await database.commit()
            emit_event("news.shortfall", market=market_code, status="unavailable", count=0)
            return edition.status
        try:
            selection_call = await _retry(
                lambda: client.select(usable, policy=spec.selection),
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
            emit_event("news.selection.failed", market=market_code, error_code=type(error).__name__)
            return edition.status
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
        edition.status, edition.caveat = _edition_status(complete_count, spec.target_items)
        await database.commit()
        emit_event(
            "news.shortfall", market=market_code, status=edition.status, count=complete_count
        )
        return edition.status


OUTCOME_SEVERITY = {"failed": 4, "unavailable": 3, "partial": 2, "idempotent": 1, "complete": 0}


async def run_all_editions(
    session_factory: async_sessionmaker[AsyncSession],
    client: DeepSeekClient,
    edition_date: date,
    *,
    allowed_hostnames: str,
    fetch_timeout_seconds: float = 25,
    discovery_timeout_seconds: float = 30,
    markets: tuple[str, ...] = EDITION_ORDER,
) -> str:
    """Run every configured edition in order and return the worst outcome.

    One edition's exception does not stop the others; it is reported as
    ``failed`` so the scheduler's same-day retry re-attempts the whole set,
    where complete editions are idempotent no-ops.
    """
    worst = "complete"
    for market_code in markets:
        spec = edition_spec(market_code)
        try:
            outcome = await run_news_edition(
                session_factory,
                client,
                edition_date,
                allowed_hostnames=allowed_hostnames,
                fetch_timeout_seconds=fetch_timeout_seconds,
                discovery_timeout_seconds=discovery_timeout_seconds,
                spec=spec,
            )
        except Exception as error:
            emit_event("news.edition.failed", market=market_code, error_code=type(error).__name__)
            outcome = "failed"
        if OUTCOME_SEVERITY[outcome] > OUTCOME_SEVERITY[worst]:
            worst = outcome
    return "unavailable" if worst == "failed" else worst
