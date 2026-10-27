"""Response cache for the model and embedding calls a replay makes.

A replay must cost nothing the second time it runs with the same settings, so
every answer is kept on disk and served again for the same request. A request
is the model, the system prompt, the user payload and the call's subject (the
article a triage call is about). Event ids in the payload are replaced by
placeholders in order of appearance before hashing, and in the stored answer,
because a triage replay opens its events afresh with new random ids: the same
window rebuilt the same way must still hit.

The same request asked again within one run (a retry after an answer that
broke the stage contract) gets the next stored answer, so retries replay as
they first happened.

A repetition number (``replay_rep``) asks for an independent draw of the same
request: repetition 1 is the key every earlier run stored, any other number is
part of the key, so a rerun with ``--rep 2`` gets fresh answers once and then
replays them for free.

A provider other than the production one (``provider.LlmProvider``, e.g.
OpenCode Go) adds its tag to the key: the endpoint and the extra request
fields, so thinking on and off never share an answer and a Go answer never
lands on a DeepSeek entry.

Answers fetched live are priced and appended to the round's ledger (``costs``)
with the provider's usage, including DeepSeek's cache-hit input tokens.

A provider that is unreachable, times out, rate-limits or answers 5xx is asked
again after a growing pause (``API_RETRY_DELAYS``). When the retries run out
the failure is recorded in ``ApiFailures`` before it propagates, so the runner
can void the market day instead of keeping what the pipeline did without that
call (a score-only fallback, or a duplicate or repeat screen silently skipped).
"""

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom.models import NewsroomLlmCall
from daily_insights_api.modules.newsroom.providers import CallAudit, Embedder, JsonModel
from daily_insights_api.modules.newsroom.queue import FatalStageError, RetryableStageError
from daily_insights_api.modules.newsroom.replay.costs import Ledger, cache_hit_tokens

ResultT = TypeVar("ResultT", bound=BaseModel)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_PLACEHOLDER = re.compile(r"<<id:(\d+)>>")
CACHE_REQUEST_ID = "replay-cache"


class ReplayCacheMiss(RuntimeError):
    """An offline replay needed an answer the cache does not have."""


# Pauses before each further attempt after a transient provider failure.
API_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0, 40.0)
_TRANSIENT = re.compile(r"_provider_(unreachable|timeout|http_429|http_5\d\d)$")


def transient(error: RetryableStageError) -> bool:
    """A provider outage rather than an answer the model got wrong."""
    return _TRANSIENT.search(error.code) is not None


@dataclass
class ApiFailures:
    """Calls whose transient provider failures outlasted every retry, in order."""

    calls: list[tuple[str, str]] = field(default_factory=list)

    def add(self, audit: CallAudit, code: str) -> None:
        self.calls.append((f"{audit.stage} {audit.prompt_version or '-'}", code))


Sleep = Callable[[float], Awaitable[None]]


async def with_retries[T](
    call: Callable[[], Awaitable[T]],
    *,
    audit: CallAudit,
    failures: ApiFailures | None,
    delays: Sequence[float] = API_RETRY_DELAYS,
    sleep: Sleep = asyncio.sleep,
) -> T:
    """``call()``, asked again after each transient failure; the last one is recorded."""
    for attempt in range(len(delays) + 1):
        try:
            return await call()
        except RetryableStageError as error:
            if not transient(error):
                raise
            if attempt == len(delays):
                if failures is not None:
                    failures.add(audit, error.code)
                raise
            await sleep(delays[attempt])
    raise AssertionError("unreachable")


_rep: ContextVar[int] = ContextVar("replay_rep", default=1)
# The usage of every provider reply while a live call is in flight.
_usage_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar("replay_usage_sink", default=None)


@contextmanager
def replay_rep(rep: int) -> Iterator[None]:
    """Answer the calls made inside (and in tasks started inside) as repetition ``rep``."""
    if rep < 1:
        raise ValueError("a repetition number starts at 1")
    token = _rep.set(rep)
    try:
        yield
    finally:
        _rep.reset(token)


def current_rep() -> int:
    return _rep.get()


async def record_usage(response: httpx.Response) -> None:
    """httpx response hook: keep the reply's ``usage`` (and served model) for pricing."""
    sink = _usage_sink.get()
    if sink is None or response.status_code >= 400:
        return
    try:
        await response.aread()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return
    if isinstance(data, dict) and isinstance(data.get("usage"), dict):
        sink.append({**data["usage"], "model": data.get("model")})


def watch_usage(client: Any) -> None:
    """Record the usage of every reply a provider client's ``httpx`` client receives.

    The provider classes keep their client private; the replay only adds a
    response hook to it, so the production request path is unchanged.
    """
    http: httpx.AsyncClient | None = getattr(client, "_client", None)
    if http is not None:
        http.event_hooks["response"] = [*http.event_hooks["response"], record_usage]


def canonical_ids(text: str) -> tuple[str, list[str]]:
    """``text`` with every UUID replaced by ``<<id:n>>``, and the UUIDs in that order."""
    ids: dict[str, int] = {}

    def replace(match: re.Match[str]) -> str:
        return f"<<id:{ids.setdefault(match.group(0), len(ids))}>>"

    return _UUID.sub(replace, text), list(ids)


def label_ids(text: str, ids: Sequence[str]) -> str:
    """Replace the UUIDs listed in ``ids`` by their placeholders; others stay."""
    index = {value: position for position, value in enumerate(ids)}
    return _UUID.sub(
        lambda match: (
            f"<<id:{index[match.group(0)]}>>" if match.group(0) in index else match.group(0)
        ),
        text,
    )


def restore_ids(text: str, ids: Sequence[str]) -> str:
    """Put this run's UUIDs back in place of the placeholders."""

    def replace(match: re.Match[str]) -> str:
        position = int(match.group(1))
        return ids[position] if position < len(ids) else match.group(0)

    return _PLACEHOLDER.sub(replace, text)


def request_key(
    *,
    model: str,
    system: str,
    payload: dict[str, Any],
    subject: uuid.UUID | None,
    rep: int = 1,
    provider: str | None = None,
) -> tuple[str, list[str]]:
    """SHA-256 of the request with its event ids replaced, and those ids.

    Repetition 1 without a provider tag hashes exactly what runs before
    repetitions and providers existed hashed.
    """
    canonical, ids = canonical_ids(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    request: dict[str, Any] = {
        "model": model,
        "system": system,
        "payload": canonical,
        "subject": None if subject is None else str(subject),
    }
    if rep != 1:
        request["rep"] = rep
    if provider is not None:
        request["provider"] = provider
    material = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest(), ids


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False)
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


@dataclass(frozen=True, slots=True)
class CachedAnswer:
    # The answer's JSON with event ids as placeholders.
    result: str
    input_tokens: int
    output_tokens: int
    # DeepSeek's cache-hit share of ``input_tokens``; None before it was recorded.
    cache_hit_tokens: int | None = None


class ResponseCache:
    """Answers under ``<root>/llm`` and title vectors under ``<root>/embed``, one file each."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _llm_path(self, key: str) -> Path:
        return self.root / "llm" / key[:2] / f"{key}.json"

    def _embed_path(self, key: str) -> Path:
        return self.root / "embed" / key[:2] / f"{key}.json"

    def answer(self, key: str, occurrence: int) -> CachedAnswer | None:
        stored = _read_json(self._llm_path(key))
        if stored is None or occurrence >= len(stored["answers"]):
            return None
        answer = stored["answers"][occurrence]
        return CachedAnswer(
            answer["result"],
            answer["input_tokens"],
            answer["output_tokens"],
            answer.get("cache_hit_tokens"),
        )

    def store_answer(
        self, key: str, occurrence: int, answer: CachedAnswer, *, model: str, audit: CallAudit
    ) -> None:
        path = self._llm_path(key)
        stored = _read_json(path) or {
            "model": model,
            "stage": audit.stage,
            "prompt_version": audit.prompt_version,
            "answers": [],
        }
        answers: list[dict[str, Any]] = stored["answers"]
        # Only the next occurrence is appended; a gap means a concurrent writer.
        if occurrence == len(answers):
            answers.append(
                {
                    "result": answer.result,
                    "input_tokens": answer.input_tokens,
                    "output_tokens": answer.output_tokens,
                    "cache_hit_tokens": answer.cache_hit_tokens,
                }
            )
            _write_json(path, stored)

    @staticmethod
    def vector_key(model: str, text: str) -> str:
        return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()

    def vector(self, model: str, text: str) -> list[float] | None:
        stored = _read_json(self._embed_path(self.vector_key(model, text)))
        return None if stored is None else [float(value) for value in stored["vector"]]

    def store_vector(self, model: str, text: str, vector: Sequence[float]) -> None:
        _write_json(
            self._embed_path(self.vector_key(model, text)),
            {"model": model, "text": text, "vector": list(vector)},
        )


@dataclass
class CallCount:
    calls: int = 0
    hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Tokens of the calls that went to the provider in this run, and their price.
    live_input_tokens: int = 0
    live_output_tokens: int = 0
    live_cache_hit_tokens: int = 0
    live_usd: float = 0.0

    @property
    def misses(self) -> int:
        return self.calls - self.hits


@dataclass
class CacheStats:
    """Calls per ``stage prompt_version``; embedding counts texts, not requests."""

    llm: dict[str, CallCount] = field(default_factory=dict)
    embed: CallCount = field(default_factory=CallCount)

    def count(self, audit: CallAudit) -> CallCount:
        label = f"{audit.stage} {audit.prompt_version or '-'}"
        return self.llm.setdefault(label, CallCount())

    def count_label(self, label: str) -> CallCount:
        """A copy of the counts under ``label`` as they stand now."""
        return replace(self.llm.get(label) or CallCount())

    def live_usd(self) -> float:
        return sum(count.live_usd for count in self.llm.values()) + self.embed.live_usd

    def as_dict(self) -> dict[str, Any]:
        def row(count: CallCount) -> dict[str, Any]:
            return {
                "calls": count.calls,
                "hits": count.hits,
                "misses": count.misses,
                "input_tokens": count.input_tokens,
                "output_tokens": count.output_tokens,
                "live_input_tokens": count.live_input_tokens,
                "live_output_tokens": count.live_output_tokens,
                "live_cache_hit_tokens": count.live_cache_hit_tokens,
                "live_usd": round(count.live_usd, 6),
            }

        return {
            "llm": {label: row(count) for label, count in sorted(self.llm.items())},
            "embed": row(self.embed),
            "live_usd": round(self.live_usd(), 6),
        }


class CallGate(Protocol):
    """Orders concurrent triage calls (``runner.TriageGate``); no-op outside a triage pass."""

    async def arrive(self) -> None: ...

    async def wait_turn(self) -> None: ...


class CachedJsonModel:
    """A ``JsonModel`` answering from the cache, and from ``live`` on a miss.

    ``live`` is built on the first miss only, so a fully cached replay never
    reads a provider key; without it a miss raises ``ReplayCacheMiss``.
    """

    def __init__(
        self,
        cache: ResponseCache,
        stats: CacheStats,
        *,
        live: Callable[[], JsonModel] | None,
        gate: CallGate | None = None,
        ledger: Ledger | None = None,
        failures: ApiFailures | None = None,
        retry_delays: Sequence[float] = API_RETRY_DELAYS,
        sleep: Sleep = asyncio.sleep,
        provider: str | None = None,
    ) -> None:
        self._cache = cache
        self._stats = stats
        self._live_factory = live
        self._live: JsonModel | None = None
        # Part of every request key (``request_key``); None for the production provider.
        self.provider = provider
        self._gate = gate
        self._ledger = ledger
        self._failures = failures
        self._retry_delays = retry_delays
        self._sleep = sleep
        self._occurrences: Counter[str] = Counter()

    async def aclose(self) -> None:
        closer: Callable[[], Awaitable[None]] | None = getattr(self._live, "aclose", None)
        if closer is not None:
            await closer()

    async def complete(
        self,
        database: AsyncSession,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        result_type: type[ResultT],
        audit: CallAudit,
    ) -> ResultT:
        gated = self._gate is not None and audit.stage == "triage"
        if gated and self._gate is not None:
            await self._gate.arrive()
        key, ids = request_key(
            model=model,
            system=system,
            payload=payload,
            subject=audit.subject_id,
            rep=current_rep(),
            provider=self.provider,
        )
        # Only an answer moves to the next occurrence: a live call that failed
        # stored nothing, so its retry must fill the same slot or it is lost.
        occurrence = self._occurrences[key]
        count = self._stats.count(audit)
        count.calls += 1
        cached = self._cache.answer(key, occurrence)
        if cached is not None:
            count.hits += 1
            result = result_type.model_validate(json.loads(restore_ids(cached.result, ids)))
            tokens = (cached.input_tokens, cached.output_tokens)
            database.add(
                NewsroomLlmCall(
                    stage=audit.stage,
                    subject_id=audit.subject_id,
                    model=model,
                    prompt_version=audit.prompt_version,
                    input_tokens=tokens[0],
                    output_tokens=tokens[1],
                    latency_ms=0,
                    request_id=CACHE_REQUEST_ID,
                )
            )
        else:
            result, tokens, cache_hit = await self._ask(
                database,
                model=model,
                system=system,
                payload=payload,
                result_type=result_type,
                audit=audit,
                count=count,
            )
            count.live_input_tokens += tokens[0]
            count.live_output_tokens += tokens[1]
            self._cache.store_answer(
                key,
                occurrence,
                CachedAnswer(
                    label_ids(result.model_dump_json(by_alias=True), ids),
                    tokens[0],
                    tokens[1],
                    cache_hit,
                ),
                model=model,
                audit=audit,
            )
        self._occurrences[key] += 1
        count.input_tokens += tokens[0]
        count.output_tokens += tokens[1]
        if gated and self._gate is not None:
            await self._gate.wait_turn()
        return result

    async def _ask(
        self,
        database: AsyncSession,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        result_type: type[ResultT],
        audit: CallAudit,
        count: CallCount,
    ) -> tuple[ResultT, tuple[int, int], int | None]:
        if self._live_factory is None:
            raise ReplayCacheMiss(f"no cached answer for a {audit.stage} call")
        if self._live is None:
            self._live = self._live_factory()
        live = self._live
        before = {id(row) for row in database.new if isinstance(row, NewsroomLlmCall)}
        usages: list[dict[str, Any]] = []
        token = _usage_sink.set(usages)
        failed = True
        error_code: str | None = None
        started = time.monotonic()
        try:
            result = await with_retries(
                lambda: live.complete(
                    database,
                    model=model,
                    system=system,
                    payload=payload,
                    result_type=result_type,
                    audit=audit,
                ),
                audit=audit,
                failures=self._failures,
                delays=self._retry_delays,
                sleep=self._sleep,
            )
            failed = False
        except (RetryableStageError, FatalStageError) as error:
            error_code = error.code
            raise
        finally:
            _usage_sink.reset(token)
            # A failed call is paid for too.
            if self._ledger is not None:
                entry = self._ledger.chat(
                    stage=f"{audit.stage} {audit.prompt_version or '-'}",
                    model=model,
                    usages=usages,
                    failed=failed,
                    latency_ms=round((time.monotonic() - started) * 1000),
                    error=error_code,
                )
                if entry is not None:
                    count.live_usd += entry.usd
                    count.live_cache_hit_tokens += entry.cache_hit_tokens or 0
        rows = [
            row
            for row in database.new
            if isinstance(row, NewsroomLlmCall) and id(row) not in before
        ]
        hits = [hit for hit in map(cache_hit_tokens, usages) if hit is not None]
        return (
            result,
            (
                sum(row.input_tokens or 0 for row in rows),
                sum(row.output_tokens or 0 for row in rows),
            ),
            sum(hits) if hits else None,
        )


class StageRouter:
    """A ``JsonModel`` sending each call to the model of its stage (``CallAudit.stage``)."""

    def __init__(self, routes: dict[str, JsonModel], default: JsonModel) -> None:
        self._routes = routes
        self._default = default

    async def aclose(self) -> None:
        for model in {id(m): m for m in (*self._routes.values(), self._default)}.values():
            closer: Callable[[], Awaitable[None]] | None = getattr(model, "aclose", None)
            if closer is not None:
                await closer()

    async def complete(
        self,
        database: AsyncSession,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        result_type: type[ResultT],
        audit: CallAudit,
    ) -> ResultT:
        return await self._routes.get(audit.stage, self._default).complete(
            database,
            model=model,
            system=system,
            payload=payload,
            result_type=result_type,
            audit=audit,
        )


class CachedEmbedder:
    """An ``Embedder`` for working titles: cached per text, the rest from ``live`` in one call."""

    def __init__(
        self,
        cache: ResponseCache,
        stats: CacheStats,
        *,
        model: str,
        live: Callable[[], Embedder] | None,
        ledger: Ledger | None = None,
        failures: ApiFailures | None = None,
        retry_delays: Sequence[float] = API_RETRY_DELAYS,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._cache = cache
        self._stats = stats
        self._model = model
        self._live_factory = live
        self._live: Embedder | None = None
        self._ledger = ledger
        self._failures = failures
        self._retry_delays = retry_delays
        self._sleep = sleep

    async def aclose(self) -> None:
        closer: Callable[[], Awaitable[None]] | None = getattr(self._live, "aclose", None)
        if closer is not None:
            await closer()

    async def embed(
        self, database: AsyncSession, texts: Sequence[str], *, audit: CallAudit
    ) -> list[list[float]]:
        vectors: dict[str, list[float]] = {}
        missing: list[str] = []
        for text in dict.fromkeys(texts):
            cached = self._cache.vector(self._model, text)
            if cached is None:
                missing.append(text)
            else:
                vectors[text] = cached
        self._stats.embed.calls += len(set(texts))
        self._stats.embed.hits += len(set(texts)) - len(missing)
        if missing:
            if self._live_factory is None:
                raise ReplayCacheMiss("no cached vector for a working title")
            if self._live is None:
                self._live = self._live_factory()
            usages: list[dict[str, Any]] = []
            token = _usage_sink.set(usages)
            live = self._live
            try:
                fetched = await with_retries(
                    lambda: live.embed(database, missing, audit=audit),
                    audit=audit,
                    failures=self._failures,
                    delays=self._retry_delays,
                    sleep=self._sleep,
                )
            finally:
                _usage_sink.reset(token)
                tokens = sum(int(usage.get("prompt_tokens") or 0) for usage in usages)
                self._stats.embed.live_input_tokens += tokens
                if self._ledger is not None and usages:
                    self._stats.embed.live_usd += self._ledger.embedding(
                        model=self._model, tokens=tokens
                    ).usd
            for text, vector in zip(missing, fetched, strict=True):
                self._cache.store_vector(self._model, text, vector)
                vectors[text] = vector
        return [vectors[text] for text in texts]
