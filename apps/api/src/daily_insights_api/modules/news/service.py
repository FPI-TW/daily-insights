import asyncio
import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.news.contracts import (
    Candidate,
    LocalizedSummary,
    SelectedCandidate,
    Selection,
)
from daily_insights_api.modules.news.editions import (
    EDITION_ORDER,
    GLOBAL_SPEC,
    EditionSpec,
    edition_spec,
)
from daily_insights_api.modules.news.extraction import (
    FetchedCandidate,
    fetch_article,
    safe_article_client,
)
from daily_insights_api.modules.news.feeds import discover_feed_candidates, feed_client
from daily_insights_api.modules.news.llm import (
    CoveredEvent,
    DeepSeekClient,
    ModelCall,
    ModelCallError,
    publishable_selection,
)
from daily_insights_api.modules.news.models import (
    NewsEdition,
    NewsGenerationAudit,
    NewsItem,
    NewsPresentation,
)

DERIVATION_VERSION = "feeds-deepseek-news.v6"
SUMMARY_PROMPT_VERSION = "summary-v3"
LOCALES = ("zh-hant", "zh-hans", "en")
TAIPEI = ZoneInfo("Asia/Taipei")
# Only a complete edition is final; partial and unavailable editions may be
# regenerated from identical inputs so a transient provider failure cannot
# freeze the day's news.
IDEMPOTENT_STATUS = "complete"
MAX_SELECTION_ROUNDS = 3
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


async def _summarize_with_retry(
    client: DeepSeekClient,
    fetched: FetchedCandidate,
    locale: str,
    on_failure: Callable[[Exception], None],
) -> ModelCall:
    feedback: str | None = None

    async def attempt() -> ModelCall:
        return await client.summarize(
            fetched.candidate, fetched.body, locale, retry_feedback=feedback
        )

    def failed(error: Exception) -> None:
        nonlocal feedback
        if isinstance(error, ModelCallError) and error.error_code.startswith("summary_"):
            feedback = error.error_code
        on_failure(error)

    return await _retry(attempt, failed)


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
        error_code=error.error_code if isinstance(error, ModelCallError) else type(error).__name__,
    )


def _interleave_by_host[T](items: list[T], host_of: Callable[[T], str]) -> list[T]:
    """Round-robin across hosts, keeping each host's own order.

    Hosts are visited in the order of their first (best-ranked) item, so the
    overall ranking still decides who leads while no single host can fill
    the list on its own.
    """
    queues: dict[str, list[T]] = {}
    for item in items:
        queues.setdefault(host_of(item), []).append(item)
    result: list[T] = []
    while queues:
        for host in list(queues):
            result.append(queues[host].pop(0))
            if not queues[host]:
                del queues[host]
    return result


def _cap_discovery(
    candidates: list[Candidate],
    *,
    per_source: int = MAX_DISCOVERY_PER_SOURCE,
    total: int | None = None,
    full_text_ids: frozenset[str] = frozenset(),
    interleave: bool = False,
) -> list[Candidate]:
    """Bound the articles fetched per source and in total, newest first.

    Candidates whose body already arrived with the feed are ranked ahead of
    the rest: they cost no fetch, so the total budget favours them. With
    ``interleave`` the budget is spread across sources in turn.
    """
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            candidate.id not in full_text_ids,
            candidate.seen_at is None,
            -(candidate.seen_at.timestamp() if candidate.seen_at else 0.0),
            str(candidate.url),
        ),
    )
    if interleave:
        ordered = _interleave_by_host(ordered, lambda candidate: candidate.hostname)
    per_host: dict[str, int] = {}
    capped: list[Candidate] = []
    for candidate in ordered:
        if per_host.get(candidate.hostname, 0) >= per_source:
            continue
        per_host[candidate.hostname] = per_host.get(candidate.hostname, 0) + 1
        capped.append(candidate)
        if total is not None and len(capped) >= total:
            break
    return capped


def _limit_candidates(
    usable: list[FetchedCandidate],
    *,
    total: int = MAX_CANDIDATES,
    per_source: int = MAX_CANDIDATES_PER_SOURCE,
    interleave: bool = False,
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
    if interleave:
        ordered = _interleave_by_host(ordered, lambda fetched: fetched.candidate.hostname)
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
    candidates: list[Candidate],
    allowed: frozenset[str],
    timeout_seconds: float = 25,
    bodies: dict[str, str] | None = None,
) -> list[FetchedCandidate]:
    """Extract article text, using feed-supplied bodies where a feed carries them."""
    semaphore = asyncio.Semaphore(6)
    supplied = bodies or {}
    async with safe_article_client(allowed, timeout_seconds) as http:

        async def fetch_one(candidate: Candidate) -> FetchedCandidate | None:
            body = supplied.get(candidate.id)
            if body:
                # Full-text feeds already passed the discovery allowlist; the
                # article page is not fetched, which also spares the publisher.
                emit_event(
                    "news.source.fetched",
                    hostname=candidate.hostname,
                    bytes=len(body),
                    full_text=True,
                )
                return FetchedCandidate(
                    candidate,
                    str(candidate.url),
                    body,
                    hashlib.sha256(body.encode()).hexdigest(),
                    candidate.seen_at,
                )
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
    allowed_hostnames: frozenset[str],
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
    allowed = allowed_hostnames
    market_code = spec.market_code
    bodies: dict[str, str] = {}
    async with feed_client(discovery_timeout_seconds) as feeds_http:
        feed_candidates = await discover_feed_candidates(
            feeds_http, allowed, market=market_code, bodies=bodies
        )
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
    candidates = _cap_discovery(
        feed_candidates,
        per_source=spec.max_discovery_per_source,
        total=spec.max_discovery_total,
        full_text_ids=frozenset(bodies),
        interleave=spec.interleave_sources,
    )
    emit_event("news.candidates.merged", market=market_code, total=len(candidates))
    usable = (
        await _fetch_usable_candidates(candidates, allowed, fetch_timeout_seconds, bodies)
        if candidates
        else []
    )
    usable = _limit_candidates(
        usable,
        total=spec.max_candidates * 2,
        per_source=spec.max_per_source,
        interleave=spec.interleave_sources,
    )
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
        selected = {fetched.candidate.id: fetched for fetched in usable}
        attempted: set[str] = set()
        reviewed: set[str] = set()
        event_keys: set[str] = set()
        successful: list[SelectedCandidate] = []
        localized: dict[str, dict[str, LocalizedSummary]] = {}
        publication = Selection(selections=())
        for round_index in range(MAX_SELECTION_ROUNDS):
            covered_sources = Counter(selected[item.id].candidate.hostname for item in successful)
            remaining = [fetched for fetched in usable if fetched.candidate.id not in attempted]
            # Explore the expanded pool before recycling unselected articles;
            # within each group prefer underrepresented sources, then freshness.
            remaining.sort(
                key=lambda fetched: (
                    fetched.candidate.id in reviewed,
                    covered_sources[fetched.candidate.hostname],
                )
            )
            batch = remaining[: spec.max_candidates]
            if not batch or len(publication.selections) >= spec.target_items:
                break
            reviewed.update(fetched.candidate.id for fetched in batch)
            previous_events = tuple(
                CoveredEvent(
                    item.event_key,
                    selected[item.id].candidate.headline,
                    selected[item.id].candidate.hostname,
                    item.topic,
                )
                for item in successful
            )

            async def select_batch(
                batch: list[FetchedCandidate] = batch,
                previous_events: tuple[CoveredEvent, ...] = previous_events,
            ) -> ModelCall:
                return await client.select(
                    batch, policy=spec.selection, previous_events=previous_events
                )

            try:
                selection_call = await _retry(
                    select_batch,
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
                emit_event(
                    "news.selection.failed",
                    market=market_code,
                    error_code=error.error_code
                    if isinstance(error, ModelCallError)
                    else type(error).__name__,
                )
                break
            if not selection_call.value.selections:
                attempted.update(fetched.candidate.id for fetched in batch)
            for selected_item in selection_call.value.selections:
                if len(publication.selections) >= spec.target_items:
                    break
                attempted.add(selected_item.id)
                if selected_item.event_key in event_keys:
                    continue
                fetched = selected[selected_item.id]
                summaries: dict[str, LocalizedSummary] = {}
                try:
                    for locale in LOCALES:

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

                        call = await _summarize_with_retry(
                            client, fetched, locale, audit_attempt_failure
                        )
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
                    successful.append(selected_item)
                    localized[selected_item.id] = summaries
                    event_keys.add(selected_item.event_key)
                    publication = publishable_selection(successful, usable, spec.selection)
                    emit_event("news.summary.succeeded", locale_count=3, rank=len(successful))
                except Exception as error:
                    emit_event(
                        "news.summary.failed",
                        hostname=fetched.candidate.hostname,
                        error_code=error.error_code
                        if isinstance(error, ModelCallError)
                        else type(error).__name__,
                    )
            emit_event(
                "news.refill.round",
                market=market_code,
                round=round_index + 1,
                published_count=len(publication.selections),
                attempted_count=len(attempted),
            )
        complete_count = len(publication.selections)
        for rank, selected_item in enumerate(publication.selections, start=1):
            fetched = selected[selected_item.id]
            summaries = localized[selected_item.id]
            item = NewsItem(
                edition_id=edition.id,
                rank=rank,
                topic=selected_item.topic,
                source_name=fetched.candidate.source_name,
                source_hostname=fetched.candidate.hostname,
                source_url=fetched.source_url,
                source_headline=fetched.candidate.headline,
                source_published_at=fetched.source_published_at,
                importance=selected_item.importance,
                content_digest=fetched.content_digest,
                numeric_facts=list(summaries["en"].numeric_facts),
                market=selected_item.market,
                event_key=selected_item.event_key,
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
    allowed_hostnames: frozenset[str],
    fetch_timeout_seconds: float = 25,
    discovery_timeout_seconds: float = 30,
    markets: tuple[str, ...] = EDITION_ORDER,
) -> str:
    """Run every configured edition in order and return the worst outcome.

    This stable scalar contract is retained for command-line callers.  Queue
    executions that need targeted retries use ``run_all_editions_with_outcomes``.
    """
    outcome, _ = await run_all_editions_with_outcomes(
        session_factory,
        client,
        edition_date,
        allowed_hostnames=allowed_hostnames,
        fetch_timeout_seconds=fetch_timeout_seconds,
        discovery_timeout_seconds=discovery_timeout_seconds,
        markets=markets,
    )
    return outcome


async def run_all_editions_with_outcomes(
    session_factory: async_sessionmaker[AsyncSession],
    client: DeepSeekClient,
    edition_date: date,
    *,
    allowed_hostnames: frozenset[str],
    fetch_timeout_seconds: float = 25,
    discovery_timeout_seconds: float = 30,
    markets: tuple[str, ...] = EDITION_ORDER,
) -> tuple[str, dict[str, str]]:
    """Run every edition and retain each market's terminal outcome.

    One edition's exception does not stop the others; it is reported as
    ``failed`` so the durable queue can retry only that market.  The returned
    aggregate keeps the historical ``failed -> unavailable`` normalization.
    """
    worst = "complete"
    outcomes: dict[str, str] = {}
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
        outcomes[market_code] = outcome
        if OUTCOME_SEVERITY[outcome] > OUTCOME_SEVERITY[worst]:
            worst = outcome
    return ("unavailable" if worst == "failed" else worst), outcomes
