import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

from daily_insights_api.modules.news.contracts import Candidate, LocalizedSummary
from daily_insights_api.modules.news.llm import DeepSeekClient, ModelCallError, ModelOutputError
from daily_insights_api.modules.news.service import _failed_audit
from daily_insights_api.modules.news.sources import FetchedCandidate


def _candidate() -> Candidate:
    return Candidate(
        id="a" * 64,
        url="https://www.reuters.com/a",
        hostname="www.reuters.com",
        source_name="Reuters",
        headline="Market move",
    )


async def test_selection_rejects_unknown_id_and_summary_rejects_fabricated_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient(
        base_url="https://api.deepseek.com", api_key="secret", model="deepseek-chat"
    )
    monkeypatch.setattr(
        client,
        "_complete",
        AsyncMock(
            return_value=(
                {
                    "selections": [
                        {
                            "id": "b" * 64,
                            "topic": "markets",
                            "event_key": "market-move",
                            "market": "global",
                            "importance": 4,
                        }
                    ]
                },
                None,
                None,
                None,
                1,
                "a" * 64,
            )
        ),
    )
    with pytest.raises(ModelOutputError, match="unknown"):
        await client.select(
            [
                FetchedCandidate(
                    _candidate(), "https://www.reuters.com/a", "Source body 10%", "c" * 64
                )
            ]
        )
    monkeypatch.setattr(
        client,
        "_complete",
        AsyncMock(
            return_value=(
                {"headline": "Gain 20%", "summary": "No basis.", "numeric_facts": []},
                None,
                None,
                None,
                1,
                "a" * 64,
            )
        ),
    )
    with pytest.raises(ModelOutputError, match="ungrounded"):
        await client.summarize(_candidate(), "Source body gained 10%.", "en")


class _FakeAsyncClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def post(self, *args: object, **kwargs: object) -> httpx.Response:
        del args, kwargs
        return self.response


async def test_provider_http_and_invalid_json_failures_keep_digest_and_latency_for_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient(
        base_url="https://api.deepseek.com", api_key="secret", model="deepseek-chat"
    )
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    failures = [
        httpx.Response(503, headers={"x-request-id": "request-http"}, request=request),
        httpx.Response(
            200,
            headers={"x-request-id": "request-json"},
            json={
                "choices": [{"message": {"content": "not-json"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
            request=request,
        ),
    ]
    for response in failures:
        monkeypatch.setattr(
            "daily_insights_api.modules.news.llm.httpx.AsyncClient",
            lambda response=response, **_: _FakeAsyncClient(response),
        )
        with pytest.raises(ModelCallError) as raised:
            await client._complete({"task": "safe test"})
        error = raised.value
        audit = _failed_audit(uuid.uuid4(), "selection", None, "f" * 64, "deepseek-chat", error)
        assert len(error.input_digest) == 64
        assert audit.input_digest == error.input_digest
        assert audit.latency_ms is not None and audit.latency_ms >= 0
        assert audit.provider_request_id == error.request_id


async def test_grounding_failure_keeps_original_call_digest_and_latency_for_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient(
        base_url="https://api.deepseek.com", api_key="secret", model="deepseek-chat"
    )
    digest = "d" * 64
    monkeypatch.setattr(
        client,
        "_complete",
        AsyncMock(
            return_value=(
                {"headline": "Gain 20%", "summary": "No basis.", "numeric_facts": []},
                "request-grounding",
                4,
                2,
                12,
                digest,
            )
        ),
    )
    with pytest.raises(ModelCallError, match="ungrounded") as raised:
        await client.summarize(_candidate(), "Source body gained 10%.", "en")
    audit = _failed_audit(uuid.uuid4(), "summary", "en", "f" * 64, "deepseek-chat", raised.value)
    assert audit.input_digest == digest
    assert audit.latency_ms == 12
    assert audit.provider_request_id == "request-grounding"


@pytest.mark.parametrize(
    ("headline", "summary", "source", "valid"),
    [
        ("市場成長20%", "市場反應平穩。", "市場成長10%。", False),
        ("公司投資3億元", "資金已到位。", "公司投資30億元。", False),
        ("市場成長20%", "資金為3億元。", "市場成長20%，資金為3億元。", True),  # noqa: RUF001
        ("市場成長3%", "表現穩定。", "市場成長30%。", False),
    ],
)
async def test_numeric_grounding_handles_chinese_adjacent_numbers_without_substrings(
    monkeypatch: pytest.MonkeyPatch,
    headline: str,
    summary: str,
    source: str,
    valid: bool,
) -> None:
    client = DeepSeekClient(
        base_url="https://api.deepseek.com", api_key="secret", model="deepseek-chat"
    )
    monkeypatch.setattr(
        client,
        "_complete",
        AsyncMock(
            return_value=(
                {"headline": headline, "summary": summary, "numeric_facts": []},
                None,
                None,
                None,
                1,
                "e" * 64,
            )
        ),
    )
    if valid:
        result = await client.summarize(_candidate(), source, "zh-hant")
        assert isinstance(result.value, LocalizedSummary)
        assert result.value.headline == headline
    else:
        with pytest.raises(ModelCallError, match="ungrounded"):
            await client.summarize(_candidate(), source, "zh-hans")
