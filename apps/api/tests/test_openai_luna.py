"""Native OpenAI contracts, without sending credentials or calls outside the tests."""

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.chat.api import _with_keepalive
from daily_insights_api.modules.chat.provider import OpenAICompatibleChatProvider
from daily_insights_api.modules.newsroom.providers import CallAudit, OpenAICompatibleJsonModel
from daily_insights_api.modules.newsroom.queue import RetryableStageError
from daily_insights_api.modules.newsroom.replay.costs import chat_cost


class Reply(BaseModel):
    answer: str


class AuditSession:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def add(self, row: Any) -> None:
        self.rows.append(row)

    def add_all(self, rows: list[Any]) -> None:
        self.rows.extend(rows)


@pytest.mark.parametrize("stage", ["triage", "editor", "analysis", "translate", "embed_translate"])
@pytest.mark.parametrize("effort", ["high", "none"])
async def test_newsroom_native_parameters_and_repair(stage: str, effort: str) -> None:
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        value = {} if len(requests) == 1 else {"answer": "繁體中文"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30},
            },
            headers={"x-request-id": "req_native"},
        )

    model = OpenAICompatibleJsonModel(
        base_url="https://api.openai.com/v1",
        api_key="test",
        timeout_seconds=240,
        extra_body={"reasoning_effort": effort},
        stage_extra_body={"editor": {"reasoning_effort": effort}},
    )
    await model._client.aclose()
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    session = AuditSession()
    try:
        reply = await model.complete(
            cast(AsyncSession, session),
            model="gpt-6-luna",
            system="Return JSON",
            payload={},
            result_type=Reply,
            audit=CallAudit(stage, None),
        )
    finally:
        await model.aclose()
    assert reply.answer == "繁體中文"
    assert len(requests) == 2
    for body in requests:
        assert body["reasoning_effort"] == effort
        assert body["max_completion_tokens"] == 32768
        assert ("temperature" in body) == (effort == "none")
        assert "max_tokens" not in body
    assert [row.error_code for row in session.rows] == [f"{stage}_schema_invalid", None]
    assert session.rows[-1].request_id == "req_native"


@pytest.mark.parametrize(
    "message,finish",
    [
        ({"content": '{"answer":"cut"}'}, "length"),
        ({"content": '{"answer":"blocked"}'}, "content_filter"),
        ({"content": None, "refusal": "refused"}, "stop"),
        ({"content": ""}, "stop"),
    ],
)
async def test_newsroom_never_accepts_incomplete_output(
    message: dict[str, Any], finish: str
) -> None:
    model = OpenAICompatibleJsonModel(
        base_url="https://api.openai.com/v1", api_key="test", timeout_seconds=240
    )
    await model._client.aclose()
    model._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [{"message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 9},
                },
            )
        )
    )
    try:
        with pytest.raises(RetryableStageError) as caught:
            await model.complete(
                cast(AsyncSession, AuditSession()),
                model="gpt-6-luna",
                system="Return JSON",
                payload={},
                result_type=Reply,
                audit=CallAudit("analysis", None),
            )
        assert caught.value.audit_rows[-1].output_tokens == 9
    finally:
        await model.aclose()


@pytest.mark.parametrize(
    "terminal", ["stop", "length", "content_filter", "refusal", "empty", "interrupted"]
)
async def test_chat_native_stream_metadata_and_failures(
    monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    requests: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = [
        {"model": "gpt-6-luna", "choices": [{"delta": {"content": "繁體中文"}}]},
        {"choices": [{"delta": {}, "finish_reason": terminal}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 42}},
    ]
    if terminal == "empty":
        events[0]["choices"] = []
    if terminal == "refusal":
        events[1]["choices"] = [{"delta": {"refusal": "refused"}}]
    data = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    if terminal != "interrupted":
        data += "data: [DONE]\n\n"

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=data, headers={"x-request-id": "req_chat"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **_: original(transport=httpx.MockTransport(handle))
    )
    provider = OpenAICompatibleChatProvider(
        base_url="https://api.openai.com/v1",
        api_key="test",
        provider="openai",
        reasoning_effort="medium",
        max_output_tokens=16384,
    )
    stream = await provider.stream(model="gpt-6-luna", messages=[], timeout_seconds=180)
    chunks: list[str] = []
    if terminal == "stop":
        chunks = [chunk async for chunk in stream]
        assert chunks == ["繁體中文"]
    else:
        with pytest.raises(ValueError):
            chunks = [chunk async for chunk in stream]
    assert requests[0]["max_completion_tokens"] == 16384
    assert requests[0]["reasoning_effort"] == "medium"
    assert "max_tokens" not in requests[0] and "temperature" not in requests[0]
    assert stream.metadata.output_tokens == 42
    assert stream.metadata.input_tokens == 12
    assert stream.metadata.provider_request_id == "req_chat"


async def test_keepalive_keeps_one_pending_read_and_closes_on_cancel() -> None:
    released = asyncio.Event()
    closed = asyncio.Event()
    reads = 0

    async def source() -> AsyncGenerator[bytes]:
        nonlocal reads
        try:
            reads += 1
            await released.wait()
            yield b"data: answer\n\n"
        finally:
            closed.set()

    output = _with_keepalive(source(), interval=0.001)
    assert await anext(output) == b": keepalive\n\n"
    assert await anext(output) == b": keepalive\n\n"
    assert reads == 1
    released.set()
    assert await anext(output) == b"data: answer\n\n"
    await output.aclose()
    assert closed.is_set()


async def test_keepalive_cancels_pending_provider_read() -> None:
    closed = asyncio.Event()

    async def source() -> AsyncGenerator[bytes]:
        try:
            await asyncio.Event().wait()
            yield b"never"
        finally:
            closed.set()

    output = _with_keepalive(source(), interval=0.001)
    assert await anext(output) == b": keepalive\n\n"
    await output.aclose()
    assert closed.is_set()


def test_luna_cost_has_no_deepseek_off_peak_discount() -> None:
    for at in (datetime(2026, 10, 30, 2, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC)):
        assert chat_cost(
            "gpt-6-luna",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_hit_tokens=500_000,
            at=at,
        ) == pytest.approx(0.86)


def test_reasoning_setting_validates_at_boundary() -> None:
    assert Settings(_env_file=None).chat_model_reasoning_effort == "medium"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, chat_model_reasoning_effort="minimal")


@pytest.mark.parametrize(
    "native,news_key,chat_key",
    [(False, "old-news", "old-chat"), (True, "new-news", "new-chat"), (True, "", "")],
)
def test_release_selects_exact_credentials_without_secret_fallback(
    native: bool, news_key: str, chat_key: str
) -> None:
    root = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        "DAILY_INSIGHTS_CHAT_MODEL_PROVIDER": "openai" if native else "deepseek",
        "DAILY_INSIGHTS_CHAT_MODEL_API_BASE_URL": "https://api.openai.com/v1"
        if native
        else "https://api.deepseek.com",
        "DAILY_INSIGHTS_NEWSROOM_LLM_BASE_URL": "https://api.openai.com/v1"
        if native
        else "https://api.deepseek.com",
        "DAILY_INSIGHTS_CHAT_MODEL_API_KEY": "old-chat",
        "DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY": "old-news",
        "DAILY_INSIGHTS_CHAT_OPENAI_API_KEY": chat_key,
        "DAILY_INSIGHTS_NEWSROOM_OPENAI_API_KEY": news_key,
    }
    script = (
        ". scripts/production/select-llm-credentials.sh\n"
        'printf "%s\\n%s" "$DAILY_INSIGHTS_NEWSROOM_LLM_API_KEY" '
        '"$DAILY_INSIGHTS_CHAT_MODEL_API_KEY"'
    )
    result = subprocess.run(
        ["sh", "-ec", script], cwd=root, env=env, capture_output=True, text=True, check=True
    )
    assert result.stdout == f"{news_key}\n{chat_key}"


def test_luna_cache_write_pricing_and_context_boundary() -> None:
    at = datetime(2026, 10, 30, tzinfo=UTC)
    assert chat_cost(
        "gpt-6-luna",
        input_tokens=100_000,
        output_tokens=100_000,
        cache_hit_tokens=50_000,
        cache_write_tokens=10_000,
        at=at,
    ) == pytest.approx(0.05575)
    assert chat_cost(
        "gpt-6-luna", input_tokens=272_000, output_tokens=0, cache_hit_tokens=0, at=at
    ) == pytest.approx(0.0272)
    assert chat_cost(
        "gpt-6-luna", input_tokens=272_001, output_tokens=0, cache_hit_tokens=0, at=at
    ) == pytest.approx(0.0544002)


async def test_keepalive_preserves_timeout_task_across_yields() -> None:
    async def source() -> AsyncGenerator[bytes]:
        async with asyncio.timeout(0.02):
            yield b"meta"
            await asyncio.Event().wait()

    stream = _with_keepalive(source(), interval=0.001)
    assert await anext(stream) == b"meta"
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            async for _ in stream:
                pass


async def test_newsroom_wall_timeout_is_retryable_and_audited() -> None:
    async def handle(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200)

    model = OpenAICompatibleJsonModel(
        base_url="https://api.openai.com/v1", api_key="test", timeout_seconds=0.01
    )
    await model._client.aclose()
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(RetryableStageError) as caught:
            await model.complete(
                cast(AsyncSession, AuditSession()),
                model="gpt-6-luna",
                system="Return JSON",
                payload={},
                result_type=Reply,
                audit=CallAudit("triage", None),
            )
        assert caught.value.code == "triage_provider_timeout"
        assert len(caught.value.audit_rows) == 1
    finally:
        await model.aclose()


def test_newsroom_lease_covers_maximum_call_and_repair_budget() -> None:
    from types import SimpleNamespace

    from daily_insights_api.modules.newsroom.worker import Runtime, collect_registrations

    settings = Settings(_env_file=None, newsroom_llm_timeout_seconds=300)
    runtime = cast(Runtime, SimpleNamespace(settings=settings, renderer=None))
    stages = collect_registrations(runtime).stages
    assert {binding.stage.name for binding in stages} >= {
        "triage",
        "embed",
        "analysis",
        "why",
        "translate",
    }
    for binding in stages:
        if binding.stage.name in {"triage", "embed", "analysis", "why", "translate"}:
            assert binding.stage.lease.total_seconds() >= 2 * 300 + 30 + 60
