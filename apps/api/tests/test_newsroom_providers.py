import json
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom.contracts import AnalysisResult
from daily_insights_api.modules.newsroom.models import NewsroomLlmCall
from daily_insights_api.modules.newsroom.providers import CallAudit, OpenAICompatibleJsonModel
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


def _model(replies: list[dict[str, Any]], requests: list[dict[str, Any]]) -> Any:
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

    model = OpenAICompatibleJsonModel(base_url="https://llm.test", api_key="k", timeout_seconds=5)
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
