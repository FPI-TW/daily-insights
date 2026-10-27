import json
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
    session_id,
)
from daily_insights_api.modules.newsroom.queue import RetryableStageError

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
