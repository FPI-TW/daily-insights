import asyncio
import hashlib
import json
import logging
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.audit.api import record_audit_event
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
    NewsCandidate,
    NewsEdition,
    NewsGenerationAudit,
    NewsItem,
    NewsPresentation,
)
from daily_insights_api.modules.operations.api import sanitize_error_code

logger = logging.getLogger("daily_insights")
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
# Market tag a manually published story receives when the model never
# classified it: the edition's own tag, which is the only one it publishes.
MANUAL_MARKET_BY_EDITION = {"global": "global", "tw_equity": "taiwan", "us_equity": "us"}


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


async def _summarize_locales(
    client: DeepSeekClient,
    fetched: FetchedCandidate,
    edition_id: uuid.UUID,
    model_name: str,
    audits: list[NewsGenerationAudit],
) -> dict[str, LocalizedSummary]:
    """Summarise one story in every locale, collecting audit rows as it goes.

    Failed attempts are appended before the exception propagates so the
    caller can still persist them; a story is publishable only when all
    three locales validated.
    """
    summaries: dict[str, LocalizedSummary] = {}
    attempt_digest = hashlib.sha256(fetched.content_digest.encode()).hexdigest()
    for locale in LOCALES:

        def audit_attempt_failure(error: Exception, locale: str = locale) -> None:
            audits.append(
                _failed_audit(
                    edition_id,
                    "summary",
                    locale,
                    attempt_digest,
                    model_name,
                    error,
                    prompt_version=SUMMARY_PROMPT_VERSION,
                )
            )

        call = await _summarize_with_retry(client, fetched, locale, audit_attempt_failure)
        assert isinstance(call.value, LocalizedSummary)
        summaries[locale] = call.value
        audits.append(
            _audit(edition_id, "summary", locale, call, model_name, SUMMARY_PROMPT_VERSION)
        )
    return summaries


@dataclass
class _CandidateRecord:
    candidate: Candidate
    stage: str = "discovered"
    drop_reason: str | None = None
    content_digest: str | None = None
    source_published_at: datetime | None = None
    ai_rank: int | None = None
    ai_topic: str | None = None
    ai_market: str | None = None
    ai_importance: int | None = None
    ai_event_key: str | None = None
    item_id: uuid.UUID | None = None


class _CandidateLedger:
    """How far each feed candidate got, recorded as the edition's candidate rows.

    Stages advance monotonically through discovery, extraction and review;
    the model's original answer is kept from the first round that returned a
    story, and the drop reason records the last decision that kept it out.
    """

    def __init__(self, discovered: list[Candidate]) -> None:
        self._records = {candidate.id: _CandidateRecord(candidate) for candidate in discovered}

    def _record(self, candidate: Candidate) -> _CandidateRecord:
        # Extraction may be stubbed with stories the feeds never listed; they
        # still belong to the edition's record.
        return self._records.setdefault(candidate.id, _CandidateRecord(candidate))

    def fetching(self, candidates: list[Candidate]) -> None:
        for candidate in candidates:
            self._record(candidate).stage = "fetch_failed"

    def extracted(self, fetched: list[FetchedCandidate]) -> None:
        for item in fetched:
            record = self._record(item.candidate)
            record.stage = "unused"
            record.content_digest = item.content_digest
            record.source_published_at = item.source_published_at

    def reviewed(self, batch: list[FetchedCandidate]) -> None:
        for item in batch:
            record = self._record(item.candidate)
            if record.stage == "unused":
                record.stage = "reviewed"

    def returned(self, call: ModelCall) -> None:
        """Record the model's own list; anything returned starts as a reserve."""
        selection = call.value
        assert isinstance(selection, Selection)
        original = call.returned or (
            *selection.selections,
            *(item for item, _ in call.rejected),
        )
        for rank, item in enumerate(original, start=1):
            record = self._records.get(item.id)
            if record is None:
                continue
            if record.ai_rank is None:
                record.ai_rank = rank
                record.ai_topic = item.topic
                record.ai_market = item.market
                record.ai_importance = item.importance
                record.ai_event_key = item.event_key
            record.stage, record.drop_reason = "dropped", "reserve"
        for item, reason in call.rejected:
            self.drop(item.id, reason)

    def drop(self, candidate_id: str, reason: str) -> None:
        record = self._records.get(candidate_id)
        if record is not None:
            record.stage, record.drop_reason = "dropped", reason

    def published(self, candidate_id: str, item_id: uuid.UUID) -> None:
        record = self._records[candidate_id]
        record.stage, record.drop_reason, record.item_id = "published", None, item_id

    def rows(self, edition_id: uuid.UUID) -> list[NewsCandidate]:
        return [
            NewsCandidate(
                edition_id=edition_id,
                candidate_id=record.candidate.id,
                source_name=record.candidate.source_name,
                hostname=record.candidate.hostname,
                url=str(record.candidate.url),
                headline=record.candidate.headline,
                seen_at=record.candidate.seen_at,
                source_published_at=record.source_published_at,
                content_digest=record.content_digest,
                stage=record.stage,
                drop_reason=record.drop_reason,
                ai_rank=record.ai_rank,
                ai_topic=record.ai_topic,
                ai_market=record.ai_market,
                ai_importance=record.ai_importance,
                ai_event_key=record.ai_event_key,
                item_id=record.item_id,
            )
            for record in self._records.values()
        ]


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
    ledger = _CandidateLedger(feed_candidates)
    candidates = _cap_discovery(
        feed_candidates,
        per_source=spec.max_discovery_per_source,
        total=spec.max_discovery_total,
        full_text_ids=frozenset(bodies),
        interleave=spec.interleave_sources,
    )
    emit_event("news.candidates.merged", market=market_code, total=len(candidates))
    ledger.fetching(candidates)
    extracted = (
        await _fetch_usable_candidates(candidates, allowed, fetch_timeout_seconds, bodies)
        if candidates
        else []
    )
    ledger.extracted(extracted)
    usable = _limit_candidates(
        extracted,
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
            database.add_all(ledger.rows(edition.id))
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
            ledger.reviewed(batch)
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
            ledger.returned(selection_call)
            if not selection_call.value.selections:
                attempted.update(fetched.candidate.id for fetched in batch)
            for selected_item in selection_call.value.selections:
                if len(publication.selections) >= spec.target_items:
                    break
                attempted.add(selected_item.id)
                if selected_item.event_key in event_keys:
                    ledger.drop(selected_item.id, "duplicate_event")
                    continue
                fetched = selected[selected_item.id]
                audits: list[NewsGenerationAudit] = []
                try:
                    summaries = await _summarize_locales(
                        client, fetched, edition.id, model_name, audits
                    )
                    successful.append(selected_item)
                    localized[selected_item.id] = summaries
                    event_keys.add(selected_item.event_key)
                    publication = publishable_selection(successful, usable, spec.selection)
                    emit_event("news.summary.succeeded", locale_count=3, rank=len(successful))
                except Exception as error:
                    ledger.drop(selected_item.id, "summary_failed")
                    emit_event(
                        "news.summary.failed",
                        hostname=fetched.candidate.hostname,
                        error_code=error.error_code
                        if isinstance(error, ModelCallError)
                        else type(error).__name__,
                    )
                finally:
                    database.add_all(audits)
            emit_event(
                "news.refill.round",
                market=market_code,
                round=round_index + 1,
                published_count=len(publication.selections),
                attempted_count=len(attempted),
            )
        complete_count = len(publication.selections)
        published_ids = {item.id for item in publication.selections}
        for successful_item in successful:
            if successful_item.id not in published_ids:
                # Summarised, but the publication could not keep it within the
                # source and diversity limits.
                ledger.drop(successful_item.id, "policy")
        for rank, selected_item in enumerate(publication.selections, start=1):
            fetched = selected[selected_item.id]
            summaries = localized[selected_item.id]
            item = _news_item(edition.id, rank, selected_item, fetched, summaries)
            database.add(item)
            await database.flush()
            database.add_all(_presentations(item.id, summaries))
            ledger.published(selected_item.id, item.id)
        edition.status, edition.caveat = _edition_status(complete_count, spec.target_items)
        database.add_all(ledger.rows(edition.id))
        await database.commit()
        emit_event(
            "news.shortfall", market=market_code, status=edition.status, count=complete_count
        )
        return edition.status


def _news_item(
    edition_id: uuid.UUID,
    rank: int,
    selected_item: SelectedCandidate,
    fetched: FetchedCandidate,
    summaries: dict[str, LocalizedSummary],
    *,
    origin: str = "model",
    published_by_user_id: uuid.UUID | None = None,
) -> NewsItem:
    return NewsItem(
        edition_id=edition_id,
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
        origin=origin,
        published_by_user_id=published_by_user_id,
    )


def _presentations(
    item_id: uuid.UUID, summaries: dict[str, LocalizedSummary]
) -> list[NewsPresentation]:
    return [
        NewsPresentation(
            item_id=item_id, locale=locale, headline=summary.headline, summary=summary.summary
        )
        for locale, summary in summaries.items()
    ]


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
    only_missing: bool = False,
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
        only_missing=only_missing,
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
    only_missing: bool = False,
) -> tuple[str, dict[str, str]]:
    """Run every edition and retain each market's terminal outcome.

    One edition's exception does not stop the others; it is reported as
    ``failed`` so the durable queue can retry only that market.  The returned
    aggregate keeps the historical ``failed -> unavailable`` normalization.

    ``only_missing`` is the automatic run's contract: a market that already
    has an edition for the date is left alone whatever its status, so a
    worker that died mid-run and was reclaimed after its lease expired
    finishes the markets it never reached instead of generating the finished
    ones a second time. The existing edition's status stands in as the outcome.
    """
    worst = "complete"
    outcomes: dict[str, str] = {}
    for market_code in markets:
        spec = edition_spec(market_code)
        if only_missing:
            existing = await _existing_edition_status(session_factory, edition_date, market_code)
            if existing is not None:
                emit_event("news.edition.skipped_existing", market=market_code, status=existing)
                outcomes[market_code] = existing
                if OUTCOME_SEVERITY[existing] > OUTCOME_SEVERITY[worst]:
                    worst = existing
                continue
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


async def _existing_edition_status(
    session_factory: async_sessionmaker[AsyncSession], edition_date: date, market_code: str
) -> str | None:
    """Status of the latest edition for the market and date, or None."""
    async with session_factory() as database:
        status = await database.scalar(
            select(NewsEdition.status)
            .where(
                NewsEdition.edition_date == edition_date,
                NewsEdition.market_code == market_code,
            )
            .order_by(NewsEdition.revision.desc())
            .limit(1)
        )
    return str(status) if status is not None else None


@dataclass(frozen=True)
class _PublishTarget:
    """The edition a manual publish adds to, captured outside any session."""

    edition_id: uuid.UUID
    edition_date: date
    market_code: str


async def _is_latest_revision(database: AsyncSession, target: _PublishTarget) -> bool:
    latest = await database.scalar(
        select(NewsEdition.id)
        .where(
            NewsEdition.edition_date == target.edition_date,
            NewsEdition.market_code == target.market_code,
        )
        .order_by(NewsEdition.revision.desc())
        .limit(1)
    )
    return latest == target.edition_id


async def _url_published(database: AsyncSession, edition_id: uuid.UUID, url: str) -> bool:
    existing = await database.scalar(
        select(NewsItem.id)
        .where(NewsItem.edition_id == edition_id, NewsItem.source_url == url)
        .limit(1)
    )
    return existing is not None


async def _refetch_candidate(
    candidate: NewsCandidate, allowed: frozenset[str], timeout_seconds: float
) -> FetchedCandidate:
    """Fetch the article again: bodies are never stored, only their digest."""
    feed_candidate = Candidate(
        id=candidate.candidate_id,
        url=candidate.url,
        hostname=candidate.hostname,
        source_name=candidate.source_name,
        headline=candidate.headline,
        seen_at=candidate.seen_at,
    )
    async with safe_article_client(allowed, timeout_seconds) as http:
        source_url, body, source_published_at = await fetch_article(http, candidate.url, allowed)
    emit_event("news.source.fetched", hostname=candidate.hostname, bytes=len(body))
    return FetchedCandidate(
        feed_candidate,
        source_url,
        body,
        hashlib.sha256(body.encode()).hexdigest(),
        source_published_at,
    )


def _manual_selection(candidate: NewsCandidate, market_code: str) -> SelectedCandidate:
    """The model's classification when it gave one, else safe edition defaults.

    The event key is only a placeholder here: the item keeps the model's
    ``ai_event_key`` (possibly null) because a made-up key would look like a
    classification that never happened.
    """
    return SelectedCandidate(
        id=candidate.candidate_id,
        topic=candidate.ai_topic or "markets",
        event_key=f"manual-{candidate.candidate_id[:16]}",
        market=candidate.ai_market or MANUAL_MARKET_BY_EDITION[market_code],
        importance=candidate.ai_importance or 3,
    )


async def _record_publish_failure(
    session_factory: async_sessionmaker[AsyncSession],
    candidate_id: uuid.UUID,
    code: str,
    audits: list[NewsGenerationAudit],
) -> None:
    async with session_factory.begin() as database:
        candidate = await database.get(NewsCandidate, candidate_id)
        if candidate is not None:
            candidate.publish_error = sanitize_error_code(code)[:500]
        database.add_all(audits)


async def _publish_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    client: DeepSeekClient,
    target: _PublishTarget,
    candidate: NewsCandidate,
    *,
    run_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    allowed: frozenset[str],
    fetch_timeout_seconds: float,
) -> str:
    """Publish one candidate; returns ``published`` or the failure code.

    Fetching and summarising happen outside any transaction so the edition's
    advisory lock is only held while the item is written.
    """
    audits: list[NewsGenerationAudit] = []
    model_name = client.model_name
    try:
        fetched = await _refetch_candidate(candidate, allowed, fetch_timeout_seconds)
    except Exception as error:
        emit_event(
            "news.source.failed", hostname=candidate.hostname, error_code=type(error).__name__
        )
        await _record_publish_failure(session_factory, candidate.id, "fetch_failed", audits)
        return "fetch_failed"
    try:
        summaries = await _summarize_locales(client, fetched, target.edition_id, model_name, audits)
    except Exception as error:
        emit_event(
            "news.summary.failed",
            hostname=candidate.hostname,
            error_code=error.error_code
            if isinstance(error, ModelCallError)
            else type(error).__name__,
        )
        await _record_publish_failure(session_factory, candidate.id, "summary_failed", audits)
        return "summary_failed"
    selection = _manual_selection(candidate, target.market_code)
    try:
        async with session_factory() as database:
            await database.execute(
                select(
                    func.pg_advisory_xact_lock(_lock_key(target.edition_date, target.market_code))
                )
            )
            current = await database.get(NewsCandidate, candidate.id, with_for_update=True)
            assert current is not None
            code = (
                "edition_superseded"
                if not await _is_latest_revision(database, target)
                else "already_published"
                if current.item_id is not None
                else "url_already_published"
                if await _url_published(database, target.edition_id, fetched.source_url)
                else None
            )
            if code is not None:
                await database.rollback()
                await _record_publish_failure(session_factory, candidate.id, code, audits)
                return code
            next_rank = await database.scalar(
                select(func.coalesce(func.max(NewsItem.rank), 0) + 1).where(
                    NewsItem.edition_id == target.edition_id
                )
            )
            assert next_rank is not None
            item = _news_item(
                target.edition_id,
                next_rank,
                selection,
                fetched,
                summaries,
                origin="manual",
                published_by_user_id=actor_user_id,
            )
            item.event_key = candidate.ai_event_key
            database.add(item)
            await database.flush()
            database.add_all(_presentations(item.id, summaries))
            database.add_all(audits)
            current.stage = "published"
            current.item_id = item.id
            current.publish_error = None
            current.content_digest = fetched.content_digest
            current.source_published_at = fetched.source_published_at
            record_audit_event(
                database,
                actor_user_id=actor_user_id,
                action="news.candidate_published",
                target_type="news_candidate",
                target_id=str(candidate.id),
                after={
                    "run_id": str(run_id),
                    "edition_id": str(target.edition_id),
                    "item_id": str(item.id),
                    "rank": next_rank,
                    "source_url": fetched.source_url,
                },
            )
            await database.commit()
    except IntegrityError:
        # A concurrent writer took the URL or rank first; the candidate keeps
        # its stage and the admin sees why.
        await _record_publish_failure(
            session_factory, candidate.id, "url_already_published", audits
        )
        return "url_already_published"
    emit_event("news.candidate.published", market=target.market_code, hostname=candidate.hostname)
    return "published"


async def publish_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    client: DeepSeekClient,
    *,
    run_id: uuid.UUID,
    edition_id: uuid.UUID,
    candidate_ids: list[uuid.UUID],
    actor_user_id: uuid.UUID | None,
    allowed_hostnames: frozenset[str],
    fetch_timeout_seconds: float,
) -> tuple[str, dict[str, object], str | None]:
    """Publish admin-chosen candidates into their edition, one commit each.

    Returns the run outcome: ``succeeded`` when every candidate was
    published, ``partial`` when some were, ``failed`` when none. The result
    maps each candidate id to ``published`` or its failure code.
    """
    async with session_factory() as database:
        edition = await database.get(NewsEdition, edition_id)
        target = (
            _PublishTarget(edition.id, edition.edition_date, edition.market_code)
            if edition is not None
            else None
        )
        candidates = (
            {
                candidate.id: candidate
                for candidate in await database.scalars(
                    select(NewsCandidate).where(
                        NewsCandidate.edition_id == edition_id,
                        NewsCandidate.id.in_(candidate_ids),
                    )
                )
            }
            if target is not None
            else {}
        )
        superseded = target is not None and not await _is_latest_revision(database, target)
        published_urls = {
            candidate.id: await _url_published(database, edition_id, candidate.url)
            for candidate in candidates.values()
        }
    outcomes: dict[str, str] = {}
    for candidate_id in candidate_ids:
        candidate = candidates.get(candidate_id)
        if target is None or candidate is None:
            outcomes[str(candidate_id)] = "candidate_not_found"
            continue
        code = (
            "edition_superseded"
            if superseded
            else "already_published"
            if candidate.item_id is not None
            else "url_already_published"
            if published_urls[candidate.id]
            else None
        )
        try:
            if code is not None:
                await _record_publish_failure(session_factory, candidate.id, code, [])
            else:
                code = await _publish_candidate(
                    session_factory,
                    client,
                    target,
                    candidate,
                    run_id=run_id,
                    actor_user_id=actor_user_id,
                    allowed=allowed_hostnames,
                    fetch_timeout_seconds=fetch_timeout_seconds,
                )
        except Exception as error:
            # One candidate's unexpected failure must not discard the outcomes
            # of the candidates already committed before it, nor stop the rest.
            emit_event(
                "news.publish.failed",
                candidate_id=str(candidate.id),
                error_code=type(error).__name__,
            )
            code = "unexpected_error"
            try:
                await _record_publish_failure(session_factory, candidate.id, code, [])
            except Exception:
                logger.exception("could not record publish failure for %s", candidate.id)
        outcomes[str(candidate_id)] = code
    published = sum(1 for code in outcomes.values() if code == "published")
    failed = len(outcomes) - published
    status = "succeeded" if published and not failed else "partial" if published else "failed"
    result: dict[str, object] = {"published": published, "failed": failed, "candidates": outcomes}
    return status, result, None if status == "succeeded" else f"news_publish_{status}"
