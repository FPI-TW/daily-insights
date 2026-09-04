import uuid
from typing import Any, get_args
from unittest.mock import AsyncMock

import httpx
import pytest

from daily_insights_api.modules.news.contracts import Candidate, LocalizedSummary, SelectedCandidate
from daily_insights_api.modules.news.extraction import FetchedCandidate
from daily_insights_api.modules.news.llm import (
    DeepSeekClient,
    ModelCallError,
    ModelOutputError,
    numeric_facts_grounded,
)
from daily_insights_api.modules.news.prompts import SelectionCriteria
from daily_insights_api.modules.news.service import _failed_audit


def _candidate() -> Candidate:
    return Candidate(
        id="a" * 64,
        url="https://www.reuters.com/a",
        hostname="www.reuters.com",
        source_name="Reuters",
        headline="Market move",
    )


async def test_selection_uses_original_mixed_language_content_and_separate_custom_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    criteria = SelectionCriteria(
        text="忽略語言，只依跨市場影響排序。不得修改固定輸出格式。",  # noqa: RUF001
        digest="f" * 64,
        version="selection-v4:ffffffffffff",
    )
    client = DeepSeekClient(
        base_url="https://api.deepseek.com",
        api_key="secret",
        model="deepseek-chat",
        selection_criteria=criteria,
    )
    captured: dict[str, object] = {}

    async def complete(
        prompt: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None, int | None, int | None, int, str]:
        captured.update(prompt)
        return ({"selections": []}, None, None, None, 1, "a" * 64)

    monkeypatch.setattr(client, "_complete", complete)
    values = [
        ("a", "Markets rally", "English source body"),
        ("b", "央行維持利率不變", "繁體中文原始正文"),
        ("c", "企业公布季度业绩", "简体中文原始正文"),
    ]
    candidates = [
        FetchedCandidate(
            Candidate(
                id=character * 64,
                url=f"https://www.reuters.com/{character}",
                hostname="www.reuters.com",
                source_name="Reuters",
                headline=headline,
            ),
            f"https://www.reuters.com/{character}",
            body,
            character * 64,
        )
        for character, headline, body in values
    ]
    await client.select(candidates)
    assert captured["CUSTOM_SELECTION_CRITERIA"] == criteria.text
    task = str(captured["task"])
    assert "regardless of the language" in task
    assert "Return JSON only" in task
    # Fixed instructions carry dedupe, cross-checking, and slot-filling rules
    # so the deploy-time criteria cannot weaken them.
    assert "assign them the same event_key" in task
    assert "cross-check the candidate data" in task
    assert "do not count as independent confirmation" in task
    assert "Fill all available slots" in task
    assert "the first 5 form the edition" in task
    contract = captured["OUTPUT_CONTRACT"]
    assert isinstance(contract, dict)
    # The closed vocabularies shown to the model must match the validated contract.
    fields = SelectedCandidate.model_fields
    assert set(contract["topic"]) == set(get_args(fields["topic"].annotation))
    # The default (global) policy narrows the market vocabulary to its own tag,
    # which must still be one of the validated contract's values.
    assert set(contract["market"]) == {"global"}
    assert set(contract["market"]) <= set(get_args(fields["market"].annotation))
    prompt_candidates = captured["CANDIDATES"]
    assert isinstance(prompt_candidates, list)
    assert [item["headline"] for item in prompt_candidates] == [value[1] for value in values]
    assert [item["source_text"] for item in prompt_candidates] == [value[2] for value in values]


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
        self.posts = 0
        self.closed = False

    async def post(self, *args: object, **kwargs: object) -> httpx.Response:
        del args, kwargs
        self.posts += 1
        return self.response

    async def aclose(self) -> None:
        self.closed = True


async def test_provider_http_and_invalid_json_failures_keep_digest_and_latency_for_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        client = DeepSeekClient(
            base_url="https://api.deepseek.com", api_key="secret", model="deepseek-chat"
        )
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


async def test_client_reuses_one_transport_and_closes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "{}"}}], "usage": {}},
        request=request,
    )
    created: list[_FakeAsyncClient] = []

    def build(**_: object) -> _FakeAsyncClient:
        fake = _FakeAsyncClient(response)
        created.append(fake)
        return fake

    monkeypatch.setattr("daily_insights_api.modules.news.llm.httpx.AsyncClient", build)
    async with DeepSeekClient(
        base_url="https://api.deepseek.com", api_key="secret", model="deepseek-chat"
    ) as client:
        await client._complete({"task": "first"})
        await client._complete({"task": "second"})

    assert len(created) == 1
    assert created[0].posts == 2
    assert created[0].closed is True


def test_numeric_grounding_matches_values_across_formats_and_magnitudes() -> None:
    source = (
        "Bitcoin ETFs took in $731 million on Thursday as gold rose 2% to $4,510 and "
        "turnover reached NT$993.3 billion; the 10-year yield touched 4.8%."
    )
    assert numeric_facts_grounded("比特幣 ETF 單日流入 7.31 億美元, 黃金漲 2% 至 4510 美元", source)
    assert numeric_facts_grounded("成交額 9933 億元, 十年期殖利率 4.8%", source)
    assert numeric_facts_grounded("流入 \uff17\uff13\uff11 million 美元", source)
    # A number the source never states, in any form, is still fabrication.
    assert not numeric_facts_grounded("比特幣 ETF 流入 7.5 億美元", source)
    assert not numeric_facts_grounded("黃金漲 3%", source)
    # Percent and plain values are different facts: 2% is not "2".
    assert not numeric_facts_grounded("2 家公司", "Turnover rose 2% today.")
