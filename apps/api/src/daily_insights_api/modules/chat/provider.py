"""Small OpenAI-compatible streaming seam used by page-context chat.

No prompt, completion, or credentials are logged here. The caller persists only
safe provider metadata after the stream has reached a terminal state.
"""

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class ChatProvider(Protocol):
    async def stream(
        self, *, model: str, messages: list[dict[str, str]], timeout_seconds: float
    ) -> "ProviderStream": ...


@dataclass
class ProviderMetadata:
    provider_request_id: str | None = None
    resolved_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None


class ProviderStream(Protocol):
    metadata: ProviderMetadata

    def __aiter__(self) -> AsyncIterator[str]: ...


class OpenAICompatibleStream:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        timeout_seconds: float,
        max_output_tokens: int = 4096,
        provider: str = "openai-compatible",
        reasoning_effort: str = "medium",
    ) -> None:
        self._provider = provider
        self._reasoning_effort = reasoning_effort
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._messages = messages
        self._timeout = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self.metadata = ProviderMetadata()

    async def __aiter__(self) -> AsyncIterator[str]:
        started = time.monotonic()
        usage: dict[str, Any] = {}
        visible = False
        finished = False
        failure: str | None = None
        body: dict[str, Any] = {
            "model": self._model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": self._messages,
        }
        if self._provider == "openai":
            body.update(
                max_completion_tokens=self._max_output_tokens,
                reasoning_effort=self._reasoning_effort,
            )
        else:
            body["max_tokens"] = self._max_output_tokens
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=httpx.Timeout(self._timeout),
                ) as response:
                    self.metadata.provider_request_id = response.headers.get("x-request-id")
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line.removeprefix("data:").strip()
                        if raw == "[DONE]":
                            finished = True
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        model = event.get("model")
                        if isinstance(model, str):
                            self.metadata.resolved_model = model
                        value = event.get("usage")
                        if isinstance(value, dict):
                            usage = value
                        choices = event.get("choices")
                        if not isinstance(choices, list) or not choices:
                            continue
                        choice = choices[0] if isinstance(choices[0], dict) else {}
                        if choice.get("finish_reason") in {"length", "content_filter"}:
                            failure = "provider_incomplete"
                        delta = choice.get("delta")
                        if isinstance(delta, dict) and delta.get("refusal"):
                            failure = "provider_refusal"
                        content = delta.get("content") if isinstance(delta, dict) else None
                        if isinstance(content, str) and content:
                            visible = visible or bool(content.strip())
                            yield content
            if failure is not None:
                raise ValueError(failure)
            if not visible or not finished:
                raise ValueError("provider_empty_or_interrupted")
        finally:
            self.metadata.input_tokens = _optional_int(usage.get("prompt_tokens"))
            self.metadata.output_tokens = _optional_int(usage.get("completion_tokens"))
            self.metadata.latency_ms = max(0, round((time.monotonic() - started) * 1000))


class OpenAICompatibleChatProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        max_output_tokens: int = 4096,
        provider: str = "openai-compatible",
        reasoning_effort: str = "medium",
    ) -> None:
        self._provider = provider
        self._reasoning_effort = reasoning_effort
        self._base_url = base_url
        self._api_key = api_key
        self._max_output_tokens = max_output_tokens

    async def stream(
        self, *, model: str, messages: list[dict[str, str]], timeout_seconds: float
    ) -> OpenAICompatibleStream:
        return OpenAICompatibleStream(
            base_url=self._base_url,
            api_key=self._api_key,
            model=model,
            messages=messages,
            timeout_seconds=timeout_seconds,
            max_output_tokens=self._max_output_tokens,
            provider=self._provider,
            reasoning_effort=self._reasoning_effort,
        )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
