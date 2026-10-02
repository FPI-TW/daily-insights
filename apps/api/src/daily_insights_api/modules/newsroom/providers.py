"""OpenAI-compatible chat (JSON mode) and embedding clients for every newsroom stage.

Both clients turn transport and provider failures into the two queue error kinds
(``RetryableStageError`` / ``FatalStageError``) and write one ``newsroom_llm_calls``
audit row per call. Stages never talk to httpx directly.
"""

import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.models import EMBEDDING_DIMENSIONS, NewsroomLlmCall
from daily_insights_api.modules.newsroom.queue import FatalStageError, RetryableStageError

ResultT = TypeVar("ResultT", bound=BaseModel)

FATAL_STATUS = {400, 401, 402, 403, 404}


@dataclass(frozen=True, slots=True)
class CallAudit:
    stage: str
    subject_id: uuid.UUID | None
    prompt_version: str | None = None


def _retry_after(response: httpx.Response) -> timedelta | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return timedelta(seconds=max(float(value), 0))
    except ValueError:
        return None


def classify_http_error(stage: str, error: Exception) -> RetryableStageError | FatalStageError:
    """Map a transport or HTTP failure onto the two queue outcomes (spec §5)."""
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in FATAL_STATUS:
            return FatalStageError(f"{stage}_provider_http_{status}")
        return RetryableStageError(
            f"{stage}_provider_http_{status}", retry_after=_retry_after(error.response)
        )
    if isinstance(error, httpx.TimeoutException):
        return RetryableStageError(f"{stage}_provider_timeout")
    return RetryableStageError(f"{stage}_provider_unreachable")


def _audit_row(
    audit: CallAudit,
    *,
    model: str,
    started: float,
    response: httpx.Response | None,
    usage: dict[str, Any] | None,
    error_code: str | None,
) -> NewsroomLlmCall:
    def _int(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return NewsroomLlmCall(
        stage=audit.stage,
        subject_id=audit.subject_id,
        model=model,
        prompt_version=audit.prompt_version,
        input_tokens=_int((usage or {}).get("prompt_tokens")),
        output_tokens=_int((usage or {}).get("completion_tokens")),
        latency_ms=int((time.monotonic() - started) * 1000),
        request_id=response.headers.get("x-request-id") if response is not None else None,
        error_code=error_code,
    )


def _fail(
    error: RetryableStageError | FatalStageError, row: NewsroomLlmCall
) -> RetryableStageError | FatalStageError:
    """Attach the audit row so it survives the stage rollback (queue.run_claimed)."""
    error.audit_rows.append(row)
    return error


class JsonModel(Protocol):
    async def complete(
        self,
        database: AsyncSession,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        result_type: type[ResultT],
        audit: CallAudit,
    ) -> ResultT: ...


class OpenAICompatibleJsonModel:
    """Chat completions in JSON mode, temperature 0, validated into ``result_type``."""

    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds, follow_redirects=False, cookies=None, trust_env=False
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenAICompatibleJsonModel":
        if settings.newsroom_llm_api_key is None:
            raise FatalStageError("newsroom_llm_configuration_missing")
        return cls(
            base_url=settings.newsroom_llm_base_url,
            api_key=settings.newsroom_llm_api_key.get_secret_value(),
            timeout_seconds=settings.newsroom_llm_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

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
        body = {
            "model": model,
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                },
            ],
        }
        started = time.monotonic()
        response: httpx.Response | None = None
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
            )
            response.raise_for_status()
        except Exception as error:
            classified = classify_http_error(audit.stage, error)
            row = _audit_row(
                audit,
                model=model,
                started=started,
                response=response,
                usage=None,
                error_code=classified.code,
            )
            raise _fail(classified, row) from error
        usage: dict[str, Any] | None = None
        try:
            data = response.json()
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
            parsed = json.loads(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError) as error:
            code = f"{audit.stage}_provider_invalid_json"
            row = _audit_row(
                audit,
                model=model,
                started=started,
                response=response,
                usage=usage,
                error_code=code,
            )
            raise _fail(RetryableStageError(code), row) from error
        try:
            result = result_type.model_validate(parsed)
        except ValidationError as error:
            code = f"{audit.stage}_schema_invalid"
            row = _audit_row(
                audit,
                model=model,
                started=started,
                response=response,
                usage=usage,
                error_code=code,
            )
            raise _fail(RetryableStageError(code), row) from error
        database.add(
            _audit_row(
                audit,
                model=model,
                started=started,
                response=response,
                usage=usage,
                error_code=None,
            )
        )
        return result


class Embedder(Protocol):
    async def embed(
        self, database: AsyncSession, texts: Sequence[str], *, audit: CallAudit
    ) -> list[list[float]]: ...


class OpenAICompatibleEmbedder:
    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds, follow_redirects=False, cookies=None, trust_env=False
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenAICompatibleEmbedder":
        if settings.newsroom_embedding_api_key is None:
            raise FatalStageError("newsroom_embedding_configuration_missing")
        return cls(
            base_url=settings.newsroom_embedding_base_url,
            api_key=settings.newsroom_embedding_api_key.get_secret_value(),
            model=settings.newsroom_embedding_model,
            timeout_seconds=settings.newsroom_embedding_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(
        self, database: AsyncSession, texts: Sequence[str], *, audit: CallAudit
    ) -> list[list[float]]:
        started = time.monotonic()
        response: httpx.Response | None = None
        try:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "input": list(texts),
                    "dimensions": EMBEDDING_DIMENSIONS,
                },
            )
            response.raise_for_status()
            data = response.json()
            vectors = [item["embedding"] for item in sorted(data["data"], key=lambda i: i["index"])]
        except (KeyError, TypeError, ValueError) as error:
            code = "embed_provider_invalid_json"
            row = _audit_row(
                audit,
                model=self._model,
                started=started,
                response=response,
                usage=None,
                error_code=code,
            )
            raise _fail(RetryableStageError(code), row) from error
        except Exception as error:
            classified = classify_http_error("embed", error)
            row = _audit_row(
                audit,
                model=self._model,
                started=started,
                response=response,
                usage=None,
                error_code=classified.code,
            )
            raise _fail(classified, row) from error
        if len(vectors) != len(texts) or any(len(v) != EMBEDDING_DIMENSIONS for v in vectors):
            raise RetryableStageError("embed_dimension_mismatch")
        database.add(
            _audit_row(
                audit,
                model=self._model,
                started=started,
                response=response,
                usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
                error_code=None,
            )
        )
        return [[float(x) for x in vector] for vector in vectors]
