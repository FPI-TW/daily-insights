import uuid
from typing import Any, get_args
from unittest.mock import AsyncMock

import httpx
import pytest

from daily_insights_api.modules.news.contracts import (
    Candidate,
    LocalizedSummary,
    SelectedCandidate,
    Selection,
)
from daily_insights_api.modules.news.extraction import FetchedCandidate
from daily_insights_api.modules.news.llm import (
    DeepSeekClient,
    ModelCall,
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
        version="selection-v6:ffffffffffff",
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
    # Relevance is a fixed gate ahead of ranking, so the deploy-time criteria
    # cannot talk the model into filling a slot with an off-market story.
    assert "Relevance to this edition is a hard gate" in task
    contract = captured["OUTPUT_CONTRACT"]
    assert isinstance(contract, dict)
    # The closed vocabularies shown to the model must match the validated contract.
    fields = SelectedCandidate.model_fields
    assert set(contract["topic"]) == set(get_args(fields["topic"].annotation))
    # Every policy offers the full market vocabulary so the model classifies
    # each story honestly; the rule then names the only tag published.
    assert set(contract["market"]) == set(get_args(fields["market"].annotation))
    assert "publishes only selections tagged 'global'" in contract["market_rule"]
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
        assert audit.error_code == (
            "provider_http_503" if response.status_code == 503 else "provider_invalid_json"
        )
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


async def test_selection_salvages_valid_stories_when_model_exceeds_domain_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient(base_url="https://api.deepseek.com", api_key="secret", model="test")
    candidates = [
        FetchedCandidate(
            _candidate().model_copy(update={"id": character * 64}),
            "https://www.reuters.com/a",
            "Source body",
            character * 64,
        )
        for character in "abc"
    ]
    complete = AsyncMock(
        return_value=(
            {
                "selections": [
                    {
                        "id": character * 64,
                        "topic": "markets",
                        "event_key": f"event-{character}",
                        "market": "global",
                        "importance": 4,
                    }
                    for character in "abc"
                ]
            },
            "request",
            10,
            10,
            5,
            "d" * 64,
        )
    )
    monkeypatch.setattr(client, "_complete", complete)
    result = await client.select(candidates)
    assert not isinstance(result.value, LocalizedSummary)
    assert [item.id for item in result.value.selections] == ["a" * 64, "b" * 64]
    assert complete.await_count == 1


async def test_summary_retry_adds_safe_feedback_and_audits_specific_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.news.service import _summarize_with_retry

    client = DeepSeekClient(base_url="https://api.deepseek.com", api_key="secret", model="test")
    captured: list[dict[str, Any]] = []

    async def complete(prompt: dict[str, Any]):  # type: ignore[no-untyped-def]
        captured.append(prompt)
        amount = "20%" if len(captured) == 1 else "10%"
        return (
            {"headline": f"Gain {amount}", "summary": "Markets move.", "numeric_facts": [amount]},
            "request",
            10,
            10,
            5,
            "d" * 64,
        )

    monkeypatch.setattr(client, "_complete", complete)
    failures: list[Exception] = []
    result = await _summarize_with_retry(
        client,
        FetchedCandidate(_candidate(), "https://www.reuters.com/a", "Gain 10%", "a" * 64),
        "en",
        failures.append,
    )
    assert isinstance(result.value, LocalizedSummary)
    assert result.value.headline == "Gain 10%"
    assert len(failures) == 1
    audit = _failed_audit(uuid.uuid4(), "summary", "en", "f" * 64, "test", failures[0])
    assert audit.error_code == "summary_ungrounded_number"
    assert audit.input_digest == "d" * 64
    assert "RETRY_GUIDANCE" not in captured[0]
    assert "Use only numbers explicitly present" in captured[1]["RETRY_GUIDANCE"]


async def test_refill_prompt_provides_covered_events_without_relaxing_market_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.news.llm import CoveredEvent

    client = DeepSeekClient(base_url="https://api.deepseek.com", api_key="secret", model="test")
    complete = AsyncMock(return_value=({"selections": []}, None, None, None, 1, "a" * 64))
    monkeypatch.setattr(client, "_complete", complete)
    await client.select(
        [], previous_events=(CoveredEvent("fed-rates", "Rates unchanged", "fed.example", "policy"),)
    )
    prompt = complete.call_args.args[0]
    assert prompt["ALREADY_COVERED_EVENTS"] == [
        {
            "event_key": "fed-rates",
            "headline": "Rates unchanged",
            "hostname": "fed.example",
            "topic": "policy",
        }
    ]
    assert "different event_key" in prompt["REFILL_GUIDANCE"]
    assert "relevance" in prompt["REFILL_GUIDANCE"]
    assert "publishes only selections tagged 'global'" in prompt["OUTPUT_CONTRACT"]["market_rule"]


async def test_market_edition_prompt_gates_relevance_and_publishes_only_its_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.news.editions import TW_EQUITY_SPEC, US_EQUITY_SPEC
    from daily_insights_api.modules.news.llm import MARKET_VALUES

    client = DeepSeekClient(base_url="https://api.deepseek.com", api_key="secret", model="test")
    complete = AsyncMock(return_value=({"selections": []}, None, None, None, 1, "a" * 64))
    monkeypatch.setattr(client, "_complete", complete)
    for spec, tag in ((TW_EQUITY_SPEC, "taiwan"), (US_EQUITY_SPEC, "us")):
        await client.select([], policy=spec.selection)
        prompt = complete.call_args.args[0]
        focus = prompt["MARKET_FOCUS"]
        assert focus.startswith("This edition covers one market only")
        assert "strong, direct link" in focus
        assert "relevance gate" in focus
        assert "empty slot is always better" in focus
        assert "Fill all 5 slots" in focus
        assert "hard gate" in prompt["task"]
        contract = prompt["OUTPUT_CONTRACT"]
        assert contract["market"] == MARKET_VALUES
        assert f"publishes only selections tagged '{tag}'" in contract["market_rule"]
        assert contract["example"]["selections"][0]["market"] == tag


async def test_select_drops_picks_the_model_tags_for_another_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.news.editions import TW_EQUITY_SPEC

    client = DeepSeekClient(base_url="https://api.deepseek.com", api_key="secret", model="test")
    candidates = [
        FetchedCandidate(
            Candidate(
                id=character * 64,
                url=f"https://news.cnyes.com/{character}",
                hostname="news.cnyes.com",
                source_name="鉅亨",
                headline=headline,
            ),
            f"https://news.cnyes.com/{character}",
            "Source body",
            character * 64,
        )
        for character, headline in (("a", "台積電上修全年展望"), ("b", "Fed 維持利率不變"))
    ]
    monkeypatch.setattr(
        client,
        "_complete",
        AsyncMock(
            return_value=(
                {
                    "selections": [
                        {
                            "id": "a" * 64,
                            "topic": "companies",
                            "event_key": "tsmc-guidance",
                            "market": "taiwan",
                            "importance": 5,
                        },
                        {
                            "id": "b" * 64,
                            "topic": "policy",
                            "event_key": "fed-decision",
                            "market": "us",
                            "importance": 4,
                        },
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
    call = await client.select(candidates, policy=TW_EQUITY_SPEC.selection)
    assert isinstance(call.value, Selection)
    # The honest "us" tag costs that pick its slot; the Taiwan story stays.
    assert [item.event_key for item in call.value.selections] == ["tsmc-guidance"]


async def test_selection_reports_rejected_picks_and_the_model_original_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The candidate record needs the model's own answer, not only what survived."""
    from daily_insights_api.modules.news.editions import GLOBAL_SPEC

    client = DeepSeekClient(base_url="https://api.deepseek.com", api_key="secret", model="test")
    candidates = [
        FetchedCandidate(
            Candidate(
                id=character * 64,
                url=f"https://www.reuters.com/{character}",
                hostname="www.reuters.com",
                source_name="Reuters",
                headline=f"Story {character}",
            ),
            f"https://www.reuters.com/{character}",
            "Source body",
            character * 64,
        )
        for character in "abcd"
    ]
    returned = [
        {
            "id": "a" * 64,
            "topic": "markets",
            "event_key": "event-a",
            "market": "asia",
            "importance": 5,
        },
        {
            "id": "b" * 64,
            "topic": "policy",
            "event_key": "event-b",
            "market": "global",
            "importance": 4,
        },
        {
            "id": "c" * 64,
            "topic": "economy",
            "event_key": "event-c",
            "market": "global",
            "importance": 3,
        },
        {
            "id": "d" * 64,
            "topic": "markets",
            "event_key": "event-d",
            "market": "global",
            "importance": 2,
        },
    ]
    monkeypatch.setattr(
        client,
        "_complete",
        AsyncMock(return_value=({"selections": returned}, None, None, None, 1, "a" * 64)),
    )
    call = await client.select(candidates, policy=GLOBAL_SPEC.selection)
    assert isinstance(call.value, Selection)
    # The global digest allows two stories per domain: "a" is off-market and
    # the third same-domain pick falls to policy repair.
    assert [item.id for item in call.value.selections] == ["b" * 64, "c" * 64]
    assert [(item.id[0], reason) for item, reason in call.rejected] == [
        ("a", "off_market"),
        ("d", "policy"),
    ]
    assert [item.id[0] for item in call.returned] == ["a", "b", "c", "d"]
    # Positional construction without the new fields keeps working.
    plain = ModelCall(call.value, None, None, None, 1, "a" * 64)
    assert plain.rejected == () and plain.returned == ()
