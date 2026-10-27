import json
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.contracts import AnalysisResult
from daily_insights_api.modules.newsroom.models import NewsroomLlmCall
from daily_insights_api.modules.newsroom.providers import (
    CallAudit,
    OpenAICompatibleJsonModel,
    classify_http_error,
    session_id,
)
from daily_insights_api.modules.newsroom.queue import (
    FatalStageError,
    RetryableStageError,
)

COMPLETE = {
    "headline": "台積電評估赴德州設新園區",
    "why": [{"market": "tw_equity", "why": "台積電為台股權值最大個股。"}],
    "related_symbols": [],
    "summary": "外媒報導台積電評估在德州興建新園區。",
}
# The failure seen in production: the model stopped right after the summary.
TRUNCATED = {"headline": COMPLETE["headline"], "summary": COMPLETE["summary"]}


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    def add_all(self, rows: list[Any]) -> None:
        self.added.extend(rows)


def _model(replies: list[dict[str, Any]], requests: list[dict[str, Any]], **options: Any) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        reply = replies[len(requests) - 1]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(reply, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    model = OpenAICompatibleJsonModel(
        base_url="https://llm.test", api_key="k", timeout_seconds=5, **options
    )
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return model


async def _complete(model: Any, session: _Session) -> AnalysisResult:
    result = await model.complete(
        cast(AsyncSession, session),
        model="deepseek-chat",
        system="system prompt",
        payload={"markets": ["tw_equity"]},
        result_type=AnalysisResult,
        audit=CallAudit("analysis", None, "v2"),
    )
    return cast(AnalysisResult, result)


async def test_schema_failure_gets_one_repair_turn_with_the_problem() -> None:
    requests: list[dict[str, Any]] = []
    session = _Session()

    result = await _complete(_model([TRUNCATED, COMPLETE], requests), session)

    assert result.why[0].market == "tw_equity"
    assert len(requests) == 2
    repair = requests[1]["messages"]
    assert [message["role"] for message in repair] == ["system", "user", "assistant", "user"]
    assert json.loads(repair[2]["content"]) == TRUNCATED
    assert "why" in repair[3]["content"]
    codes = [row.error_code for row in session.added if isinstance(row, NewsroomLlmCall)]
    assert codes == ["analysis_schema_invalid", None]


async def test_a_second_invalid_reply_is_retryable_and_keeps_both_audit_rows() -> None:
    requests: list[dict[str, Any]] = []
    session = _Session()

    with pytest.raises(RetryableStageError) as raised:
        await _complete(_model([TRUNCATED, TRUNCATED], requests), session)

    assert raised.value.code == "analysis_schema_invalid"
    assert len(requests) == 2
    assert [row.error_code for row in raised.value.audit_rows] == ["analysis_schema_invalid"] * 2
    assert session.added == []


async def test_a_valid_first_reply_makes_a_single_call() -> None:
    requests: list[dict[str, Any]] = []
    session = _Session()

    await _complete(_model([COMPLETE], requests), session)

    assert len(requests) == 1
    assert [row.error_code for row in session.added] == [None]


def _header_model(seen: list[httpx.Headers], **options: Any) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(COMPLETE, ensure_ascii=False)}}]},
        )

    model = OpenAICompatibleJsonModel(
        base_url="https://llm.test", api_key="k", timeout_seconds=5, **options
    )
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return model


async def _complete_as(model: Any, audit: CallAudit) -> None:
    await model.complete(
        cast(AsyncSession, _Session()),
        model="deepseek-v4.1-flash",
        system="system prompt",
        payload={"markets": ["tw_equity"]},
        result_type=AnalysisResult,
        audit=audit,
    )


async def test_calls_name_the_client_and_share_a_session_per_stage_and_prompt() -> None:
    seen: list[httpx.Headers] = []
    model = _header_model(
        seen, user_agent="daily-insights-newsroom/1.0", session_header="x-opencode-session"
    )

    await _complete_as(model, CallAudit("analysis", None, "v2"))
    await _complete_as(model, CallAudit("analysis", None, "v2"))
    await _complete_as(model, CallAudit("triage", None, "v2"))

    assert [headers["user-agent"] for headers in seen] == ["daily-insights-newsroom/1.0"] * 3
    assert seen[0]["authorization"] == "Bearer k"
    sessions = [headers["x-opencode-session"] for headers in seen]
    assert sessions[0] == sessions[1] == session_id(CallAudit("analysis", None, "v2"))
    assert sessions[2] != sessions[0]


async def test_no_session_header_is_sent_unless_configured() -> None:
    seen: list[httpx.Headers] = []

    await _complete_as(_header_model(seen), CallAudit("analysis", None, "v2"))

    assert "x-opencode-session" not in seen[0]
    assert seen[0]["user-agent"].startswith("python-httpx/")


THINKING_OFF = {"thinking": {"type": "disabled"}}


async def test_without_extra_fields_the_body_is_the_plain_json_mode_request() -> None:
    requests: list[dict[str, Any]] = []

    await _complete(_model([COMPLETE], requests), _Session())

    assert list(requests[0]) == ["model", "response_format", "temperature", "messages"]
    assert requests[0]["model"] == "deepseek-chat"
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["temperature"] == 0


async def test_extra_fields_reach_every_call_including_the_repair_turn() -> None:
    requests: list[dict[str, Any]] = []

    await _complete(_model([TRUNCATED, COMPLETE], requests, extra_body=THINKING_OFF), _Session())

    assert len(requests) == 2
    for body in requests:
        assert body["thinking"] == {"type": "disabled"}
        assert body["model"] == "deepseek-chat"
        assert body["response_format"] == {"type": "json_object"}
        assert body["temperature"] == 0
    assert len(requests[1]["messages"]) == 4


async def test_the_settings_extra_fields_are_sent() -> None:
    requests: list[dict[str, Any]] = []
    settings = Settings(
        _env_file=None,
        newsroom_llm_api_key=SecretStr("k"),
        newsroom_llm_extra_body=THINKING_OFF,
    )
    model = OpenAICompatibleJsonModel.from_settings(settings)
    stub = _model([COMPLETE], requests)
    await model.aclose()
    model._client = stub._client

    await _complete(model, _Session())

    assert requests[0]["thinking"] == {"type": "disabled"}


def test_extra_fields_are_read_from_a_json_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAILY_INSIGHTS_NEWSROOM_LLM_EXTRA_BODY", json.dumps(THINKING_OFF))
    assert Settings(_env_file=None).newsroom_llm_extra_body == THINKING_OFF

    monkeypatch.setenv("DAILY_INSIGHTS_NEWSROOM_LLM_EXTRA_BODY", "")
    assert Settings(_env_file=None).newsroom_llm_extra_body == {}

    monkeypatch.delenv("DAILY_INSIGHTS_NEWSROOM_LLM_EXTRA_BODY")
    assert Settings(_env_file=None).newsroom_llm_extra_body == {}


@pytest.mark.parametrize("value", ['["thinking"]', "thinking=disabled", '"disabled"'])
def test_extra_fields_must_be_a_json_object(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("DAILY_INSIGHTS_NEWSROOM_LLM_EXTRA_BODY", value)

    with pytest.raises(ValidationError, match="newsroom_llm_extra_body"):
        Settings(_env_file=None)


@pytest.mark.parametrize("field", ["model", "messages", "response_format", "temperature", "stream"])
def test_extra_fields_cannot_replace_the_request_the_client_owns(field: str) -> None:
    with pytest.raises(ValidationError, match=f"newsroom_llm_extra_body cannot set {field}"):
        Settings(_env_file=None, newsroom_llm_extra_body={**THINKING_OFF, field: True})


GO_LOG_ID = "01a17b3f-c8fe-7127-9ee1-7d265c42eac9"
# The envelope OpenCode Go uses for its own refusals (seen live for MissingSessionID).
GO_USAGE_LIMIT = {
    "type": "error",
    "error": {"type": "GoUsageLimitError", "message": "Subscription quota exceeded."},
}


def _status_error(status: int, body: Any, headers: dict[str, str] | None = None) -> Exception:
    request = httpx.Request("POST", "https://llm.test/chat/completions")
    response = httpx.Response(status, json=body, headers=headers, request=request)
    return httpx.HTTPStatusError("failed", request=request, response=response)


@pytest.mark.parametrize("status", [429, 402])
def test_a_spent_go_allowance_waits_for_retry_after(status: int) -> None:
    error = classify_http_error(
        "triage", _status_error(status, GO_USAGE_LIMIT, {"retry-after": "7200"})
    )

    assert isinstance(error, RetryableStageError)
    assert error.code == "triage_provider_usage_limit"
    assert error.retry_after == timedelta(hours=2)
    assert not error.counts_attempt


def test_a_spent_go_allowance_without_retry_after_leaves_the_wait_to_the_queue() -> None:
    error = classify_http_error("triage", _status_error(429, GO_USAGE_LIMIT))

    assert isinstance(error, RetryableStageError)
    assert error.retry_after is None
    assert not error.counts_attempt


def test_other_provider_refusals_keep_their_status_codes() -> None:
    throttled = classify_http_error(
        "triage",
        _status_error(
            429,
            {"error": {"type": "rate_limit_error", "message": "Too many requests"}},
            {"retry-after": "30"},
        ),
    )
    missing_session = classify_http_error(
        "triage",
        _status_error(
            400, {"type": "error", "error": {"type": "MissingSessionID", "message": "missing"}}
        ),
    )
    bad_key = classify_http_error(
        "triage",
        _status_error(
            401,
            {"error": {"type": "server_error", "message": "Upstream request failed: Invalid"}},
        ),
    )
    unpaid = classify_http_error("triage", _status_error(402, {"error": "Insufficient Balance"}))

    assert isinstance(throttled, RetryableStageError)
    assert (throttled.code, throttled.retry_after) == (
        "triage_provider_http_429",
        timedelta(seconds=30),
    )
    assert throttled.counts_attempt
    assert [type(error) for error in (missing_session, bad_key, unpaid)] == [FatalStageError] * 3
    assert [error.code for error in (missing_session, bad_key, unpaid)] == [
        "triage_provider_http_400",
        "triage_provider_http_401",
        "triage_provider_http_402",
    ]


def _replying_model(response: httpx.Response) -> Any:
    model = OpenAICompatibleJsonModel(base_url="https://llm.test", api_key="k", timeout_seconds=5)
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response))
    return model


async def test_go_calls_are_audited_with_the_opencode_log_id() -> None:
    session = _Session()
    answer = httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(COMPLETE, ensure_ascii=False)}}]},
        headers={"x-opencode-log-id": GO_LOG_ID},
    )

    await _complete(_replying_model(answer), session)

    assert [row.request_id for row in session.added] == [GO_LOG_ID]


async def test_a_spent_go_allowance_is_audited_and_retryable() -> None:
    refusal = httpx.Response(
        429, json=GO_USAGE_LIMIT, headers={"x-opencode-log-id": GO_LOG_ID, "retry-after": "60"}
    )

    with pytest.raises(RetryableStageError) as raised:
        await _complete(_replying_model(refusal), _Session())

    assert raised.value.code == "analysis_provider_usage_limit"
    assert [(row.error_code, row.request_id) for row in raised.value.audit_rows] == [
        ("analysis_provider_usage_limit", GO_LOG_ID)
    ]


async def test_x_request_id_still_wins_over_the_opencode_log_id() -> None:
    session = _Session()
    answer = httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(COMPLETE, ensure_ascii=False)}}]},
        headers={"x-request-id": "req-1", "x-opencode-log-id": GO_LOG_ID},
    )

    await _complete(_replying_model(answer), session)

    assert [row.request_id for row in session.added] == ["req-1"]


def _go_worker(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "runtime_role": "newsroom-worker",
        "newsroom_enabled": True,
        "newsroom_llm_base_url": "https://opencode.ai/inference/go/openai/v1",
        "newsroom_llm_api_key": SecretStr("go-key"),
        "newsroom_embedding_api_key": SecretStr("embedding-key"),
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("role", ["newsroom-worker", "orchestration-worker", "api"])
def test_an_opencode_runtime_needs_the_session_header(role: str) -> None:
    assert _go_worker(
        runtime_role=role, newsroom_llm_session_header="x-opencode-session"
    ).newsroom_enabled

    for header in (None, ""):
        with pytest.raises(ValidationError, match="newsroom_llm_session_header is required"):
            _go_worker(runtime_role=role, newsroom_llm_session_header=header)


def test_a_disabled_newsroom_or_deepseek_needs_no_session_header() -> None:
    assert not _go_worker(newsroom_enabled=False).newsroom_enabled
    assert _go_worker(newsroom_llm_base_url="https://api.deepseek.com").newsroom_enabled


def test_settings_without_the_llm_key_are_not_checked() -> None:
    # The replay reads the base URL from the production container but keeps the
    # key out of its settings.
    settings = _go_worker(runtime_role="api", newsroom_llm_api_key=None)

    assert settings.newsroom_llm_session_header is None


THINKING_HIGH = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}


def _calls_by_stage(
    settings: Settings, seen: list[tuple[dict[str, Any], dict[str, Any]]]
) -> OpenAICompatibleJsonModel:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((json.loads(request.content), request.extensions["timeout"]))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(COMPLETE, ensure_ascii=False)}}]},
        )

    model = OpenAICompatibleJsonModel.from_settings(settings, api_key="replay-key")
    model._client = httpx.AsyncClient(
        timeout=settings.newsroom_llm_timeout_seconds, transport=httpx.MockTransport(handler)
    )
    return model


async def test_editor_calls_get_their_own_extra_fields_and_timeout() -> None:
    seen: list[tuple[dict[str, Any], dict[str, Any]]] = []
    settings = Settings(
        _env_file=None,
        newsroom_llm_extra_body={**THINKING_OFF, "max_tokens": 4000},
        newsroom_editor_extra_body=THINKING_HIGH,
        newsroom_editor_timeout_seconds=240,
    )
    model = _calls_by_stage(settings, seen)

    await _complete_as(model, CallAudit("editor", None, "editor-v2"))
    await _complete_as(model, CallAudit("triage", None, "v2"))

    (editor_body, editor_timeout), (triage_body, triage_timeout) = seen
    assert editor_body["thinking"] == {"type": "enabled"}
    assert editor_body["reasoning_effort"] == "high"
    assert editor_body["max_tokens"] == 4000
    assert editor_body["response_format"] == {"type": "json_object"}
    assert triage_body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in triage_body
    assert editor_timeout["read"] == 240
    assert triage_timeout["read"] == 90


async def test_empty_editor_settings_inherit_the_shared_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAILY_INSIGHTS_NEWSROOM_EDITOR_EXTRA_BODY", "")
    monkeypatch.setenv("DAILY_INSIGHTS_NEWSROOM_EDITOR_TIMEOUT_SECONDS", "")
    settings = Settings(_env_file=None, newsroom_llm_extra_body=THINKING_OFF)
    assert settings.newsroom_editor_extra_body == {}
    assert settings.newsroom_editor_timeout_seconds is None
    seen: list[tuple[dict[str, Any], dict[str, Any]]] = []
    model = _calls_by_stage(settings, seen)

    await _complete_as(model, CallAudit("editor", None, "editor-v2"))
    await _complete_as(model, CallAudit("triage", None, "v2"))

    (editor_body, editor_timeout), (triage_body, triage_timeout) = seen
    assert {key: value for key, value in editor_body.items() if key != "messages"} == {
        key: value for key, value in triage_body.items() if key != "messages"
    }
    assert editor_timeout == triage_timeout


def test_editor_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_INSIGHTS_NEWSROOM_EDITOR_EXTRA_BODY", json.dumps(THINKING_HIGH))
    monkeypatch.setenv("DAILY_INSIGHTS_NEWSROOM_EDITOR_TIMEOUT_SECONDS", "300")

    settings = Settings(_env_file=None)

    assert settings.newsroom_editor_extra_body == THINKING_HIGH
    assert settings.newsroom_editor_timeout_seconds == 300


@pytest.mark.parametrize("value", [0, 301])
def test_the_editor_timeout_is_bounded(value: float) -> None:
    with pytest.raises(ValidationError, match="newsroom_editor_timeout_seconds"):
        Settings(_env_file=None, newsroom_editor_timeout_seconds=value)


@pytest.mark.parametrize("field", ["model", "messages", "response_format", "temperature", "stream"])
def test_editor_extra_fields_cannot_replace_the_request_the_client_owns(field: str) -> None:
    with pytest.raises(ValidationError, match=f"newsroom_editor_extra_body cannot set {field}"):
        Settings(_env_file=None, newsroom_editor_extra_body={**THINKING_HIGH, field: True})


async def test_a_reasoning_reply_is_parsed_from_content_alone() -> None:
    session = _Session()
    reply = httpx.Response(
        200,
        json={
            "model": "deepseek-v4.1-flash",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "The why list needs one entry per market.",
                        "content": json.dumps(COMPLETE, ensure_ascii=False),
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 900,
                "completion_tokens": 410,
                "completion_tokens_details": {"reasoning_tokens": 300},
                "prompt_tokens_details": {"cached_tokens": 640},
                "cost": 0,
                "opencode": {"billing_source": "go", "list_cost": 0.0004},
            },
        },
    )

    result = await _complete(_replying_model(reply), session)

    assert result.headline == COMPLETE["headline"]
    assert [(row.input_tokens, row.output_tokens) for row in session.added] == [(900, 410)]


async def test_the_replay_key_replaces_the_missing_settings_key() -> None:
    seen: list[httpx.Headers] = []
    settings = Settings(_env_file=None, newsroom_llm_session_header="x-opencode-session")
    with pytest.raises(FatalStageError, match="newsroom_llm_configuration_missing"):
        OpenAICompatibleJsonModel.from_settings(settings)
    model = OpenAICompatibleJsonModel.from_settings(settings, api_key="replay-key")
    await model.aclose()
    model._client = _header_model(seen)._client

    await _complete_as(model, CallAudit("editor", None, "editor-v2"))

    assert seen[0]["authorization"] == "Bearer replay-key"
    assert seen[0]["user-agent"] == "daily-insights-newsroom/1.0"
    assert seen[0]["x-opencode-session"] == session_id(CallAudit("editor", None, "editor-v2"))
