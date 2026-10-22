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
"""

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom.models import NewsroomLlmCall
from daily_insights_api.modules.newsroom.providers import CallAudit, Embedder, JsonModel

ResultT = TypeVar("ResultT", bound=BaseModel)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_PLACEHOLDER = re.compile(r"<<id:(\d+)>>")
CACHE_REQUEST_ID = "replay-cache"


class ReplayCacheMiss(RuntimeError):
    """An offline replay needed an answer the cache does not have."""


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
    *, model: str, system: str, payload: dict[str, Any], subject: uuid.UUID | None
) -> tuple[str, list[str]]:
    """SHA-256 of the request with its event ids replaced, and those ids."""
    canonical, ids = canonical_ids(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    material = json.dumps(
        {
            "model": model,
            "system": system,
            "payload": canonical,
            "subject": None if subject is None else str(subject),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
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
        return CachedAnswer(answer["result"], answer["input_tokens"], answer["output_tokens"])

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
    # Tokens of the calls that went to the provider in this run.
    live_input_tokens: int = 0
    live_output_tokens: int = 0

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

    def as_dict(self) -> dict[str, Any]:
        def row(count: CallCount) -> dict[str, int]:
            return {
                "calls": count.calls,
                "hits": count.hits,
                "misses": count.misses,
                "input_tokens": count.input_tokens,
                "output_tokens": count.output_tokens,
                "live_input_tokens": count.live_input_tokens,
                "live_output_tokens": count.live_output_tokens,
            }

        return {
            "llm": {label: row(count) for label, count in sorted(self.llm.items())},
            "embed": row(self.embed),
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
    ) -> None:
        self._cache = cache
        self._stats = stats
        self._live_factory = live
        self._live: JsonModel | None = None
        self._gate = gate
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
            model=model, system=system, payload=payload, subject=audit.subject_id
        )
        occurrence = self._occurrences[key]
        self._occurrences[key] += 1
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
            result, tokens = await self._ask(
                database,
                model=model,
                system=system,
                payload=payload,
                result_type=result_type,
                audit=audit,
            )
            count.live_input_tokens += tokens[0]
            count.live_output_tokens += tokens[1]
            self._cache.store_answer(
                key,
                occurrence,
                CachedAnswer(
                    label_ids(result.model_dump_json(by_alias=True), ids), tokens[0], tokens[1]
                ),
                model=model,
                audit=audit,
            )
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
    ) -> tuple[ResultT, tuple[int, int]]:
        if self._live_factory is None:
            raise ReplayCacheMiss(f"no cached answer for a {audit.stage} call")
        if self._live is None:
            self._live = self._live_factory()
        before = {id(row) for row in database.new if isinstance(row, NewsroomLlmCall)}
        result = await self._live.complete(
            database,
            model=model,
            system=system,
            payload=payload,
            result_type=result_type,
            audit=audit,
        )
        rows = [
            row
            for row in database.new
            if isinstance(row, NewsroomLlmCall) and id(row) not in before
        ]
        return result, (
            sum(row.input_tokens or 0 for row in rows),
            sum(row.output_tokens or 0 for row in rows),
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
    ) -> None:
        self._cache = cache
        self._stats = stats
        self._model = model
        self._live_factory = live
        self._live: Embedder | None = None

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
            for text, vector in zip(
                missing, await self._live.embed(database, missing, audit=audit), strict=True
            ):
                self._cache.store_vector(self._model, text, vector)
                vectors[text] = vector
        return [vectors[text] for text in texts]
