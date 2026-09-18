import uuid
from datetime import UTC, datetime
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
    source_published_at = datetime(2026, 9, 10, 8, 30, tzinfo=UTC)
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
            source_published_at,
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
    assert "5-star publication has no quantity cap" in task
    assert "up to 10 qualifying 4-star stories" in task
    assert "at most 5 stories rated 1 to 3 combined" in task
    # Relevance is a fixed gate ahead of ranking, so the deploy-time criteria
    # cannot talk the model into filling a slot with an off-market story.
    assert "Relevance to this edition is a hard gate" in task
    # Importance ranks first: every 5 before any 4, honestly rated.
    assert "Importance is the primary ranking key" in task
    assert "every 5 precedes every 4" in task
    contract = captured["OUTPUT_CONTRACT"]
    assert isinstance(contract, dict)
    assert "ordered by importance from 5 down to 1" in contract["selections"]
    assert "independently reported alternative" in contract["reserves"]
    assert "same event_key" in contract["reserves"]
    assert "absolute scale" in contract["importance"]
    assert "systemic catalyst" in contract["importance"]
    assert "official forward guidance" in contract["importance"]
    assert "investment-bank" in contract["importance"]
    assert "Pure price action" in contract["importance"]
    global_focus = str(captured["MARKET_FOCUS"])
    assert "independent dominant macro themes" in global_focus
    assert "sovereign debt supply" in global_focus
    assert "Pure price-action reports" in global_focus
    assert "company transaction" in global_focus
    assert "high-credibility consensus" in global_focus
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
    assert [item["source_published_at"] for item in prompt_candidates] == [
        source_published_at.isoformat()
    ] * len(values)


async def test_selection_retry_adds_fixed_safe_contract_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient(
        base_url="https://api.deepseek.com", api_key="secret", model="deepseek-chat"
    )
    captured: dict[str, Any] = {}

    async def complete(
        prompt: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None, int | None, int | None, int, str]:
        captured.update(prompt)
        return ({"selections": []}, None, None, None, 1, "a" * 64)

    monkeypatch.setattr(client, "_complete", complete)

    await client.select([], retry_feedback="selection_invalid_json")

    guidance = str(captured["RETRY_GUIDANCE"])
    assert "previous selection failed validation" in guidance
    assert "exactly the OUTPUT_CONTRACT" in guidance
    assert "selection_invalid_json" not in guidance


async def test_selection_schema_failure_records_only_safe_validation_issues(
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
                            "id": "a" * 64,
                            "topic": "markets",
                            "event_key": "?",
                            "market": "global",
                            "importance": 4,
                        }
                    ]
                },
                None,
                10,
                5,
                1,
                "a" * 64,
            )
        ),
    )

    with pytest.raises(ModelCallError) as captured:
        await client.select([])

    assert captured.value.error_code == "selection_schema_invalid"
    assert captured.value.validation_issues == ("selections.0.event_key:string_pattern_mismatch",)
    assert "?" not in str(captured.value.validation_issues)


async def test_selection_rejects_unknown_id_and_summary_does_not_validate_numbers(
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
    summary = await client.summarize(_candidate(), "Source body gained 10%.", "en")
    assert summary.value == LocalizedSummary(headline="Gain 20%", summary="No basis.")


async def test_selection_preserves_one_generation_reserve_for_the_same_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient(
        base_url="https://api.deepseek.com", api_key="secret", model="deepseek-chat"
    )
    primary = _candidate()
    reserve = Candidate(
        id="b" * 64,
        url="https://apnews.com/b",
        hostname="apnews.com",
        source_name="AP",
        headline=primary.headline,
    )
    response = {
        "selections": [
            {
                "id": primary.id,
                "topic": "policy",
                "event_key": "fed-rate-decision",
                "market": "global",
                "importance": 5,
            }
        ],
        "reserves": [
            {
                "id": reserve.id,
                "topic": "policy",
                "event_key": "fed-rate-decision",
                "market": "global",
                "importance": 5,
            }
        ],
    }
    monkeypatch.setattr(
        client,
        "_complete",
        AsyncMock(return_value=(response, None, None, None, 1, "a" * 64)),
    )

    result = await client.select(
        [
            FetchedCandidate(primary, str(primary.url), "Primary report", "c" * 64),
            FetchedCandidate(reserve, str(reserve.url), "Independent report", "d" * 64),
        ]
    )

    assert isinstance(result.value, Selection)
    assert [item.id for item in result.value.selections] == [primary.id]
    assert [item.id for item in result.value.reserves] == [reserve.id]
    assert [item.id for item in result.returned] == [primary.id, reserve.id]

    invalid = {**response, "reserves": [{**response["reserves"][0], "event_key": "other-event"}]}
    monkeypatch.setattr(
        client,
        "_complete",
        AsyncMock(return_value=(invalid, None, None, None, 1, "a" * 64)),
    )
    with pytest.raises(ModelCallError, match="reserve") as captured:
        await client.select(
            [
                FetchedCandidate(primary, str(primary.url), "Primary report", "c" * 64),
                FetchedCandidate(reserve, str(reserve.url), "Independent report", "d" * 64),
            ]
        )
    assert captured.value.error_code == "selection_invalid_candidate"


async def test_translation_uses_validated_zh_hant_summary_and_original_source_grounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient(
        base_url="https://api.deepseek.com", api_key="secret", model="deepseek-chat"
    )
    captured: dict[str, Any] = {}

    async def complete(
        prompt: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None, int | None, int | None, int, str]:
        captured.update(prompt)
        return (
            {
                "headline": "Markets gain 10%",
                "summary": "The move was 10%.",
                "numeric_facts": ["10%"],
            },
            None,
            None,
            None,
            1,
            "a" * 64,
        )

    monkeypatch.setattr(client, "_complete", complete)
    source_summary = LocalizedSummary(
        headline="市場上漲10%", summary="市場漲幅為10%。", numeric_facts=("10%",)
    )

    result = await client.translate(_candidate(), "The market gained 10%.", source_summary, "en")

    assert result.value == LocalizedSummary(
        headline="Markets gain 10%", summary="The move was 10%.", numeric_facts=("10%",)
    )
    assert captured["BASE_SUMMARY"] == source_summary.model_dump(mode="json")
    assert captured["ORIGINAL_SOURCE_BEGIN"] == "The market gained 10%."
    assert "untrusted quoted data" in str(captured["task"])


async def test_translation_does_not_validate_numbers_against_original_article(
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
                    "headline": "Markets gain 20%",
                    "summary": "The move was 20%.",
                    "numeric_facts": ["20%"],
                },
                None,
                None,
                None,
                1,
                "b" * 64,
            )
        ),
    )

    translated = await client.translate(
        _candidate(),
        "The market gained 10%.",
        LocalizedSummary(headline="市場上漲10%", summary="市場漲幅為10%。"),
        "en",
    )
    assert translated.value == LocalizedSummary(
        headline="Markets gain 20%", summary="The move was 20%.", numeric_facts=("20%",)
    )


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


async def test_summary_with_changed_number_keeps_call_metadata(
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
    call = await client.summarize(_candidate(), "Source body gained 10%.", "en")
    assert call.input_digest == digest
    assert call.latency_ms == 12
    assert call.request_id == "request-grounding"


@pytest.mark.parametrize(
    ("headline", "summary", "source"),
    [
        ("市場成長20%", "市場反應平穩。", "市場成長10%。"),
        ("公司投資3億元", "資金已到位。", "公司投資30億元。"),
        ("市場成長20%", "資金為3億元。", "市場成長20%，資金為3億元。"),  # noqa: RUF001
        ("市場成長3%", "表現穩定。", "市場成長30%。"),
    ],
)
async def test_summary_does_not_compare_chinese_adjacent_numbers(
    monkeypatch: pytest.MonkeyPatch,
    headline: str,
    summary: str,
    source: str,
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
    result = await client.summarize(_candidate(), source, "zh-hant")
    assert isinstance(result.value, LocalizedSummary)
    assert result.value.headline == headline


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
    candidates.append(
        FetchedCandidate(
            _candidate().model_copy(
                update={"id": "d" * 64, "hostname": "apnews.com", "source_name": "AP"}
            ),
            "https://apnews.com/d",
            "Independent fallback",
            "d" * 64,
        )
    )
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
                ],
                "reserves": [
                    {
                        "id": "d" * 64,
                        "topic": "markets",
                        "event_key": "event-c",
                        "market": "global",
                        "importance": 5,
                    }
                ],
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
    assert result.value.reserves == ()
    assert {item.id for item, reason in result.rejected if reason == "policy"} == {
        "c" * 64,
        "d" * 64,
    }
    assert complete.await_count == 1


async def test_summary_retry_adds_safe_feedback_and_audits_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.news.service import _summarize_with_retry

    client = DeepSeekClient(base_url="https://api.deepseek.com", api_key="secret", model="test")
    captured: list[dict[str, Any]] = []

    async def complete(prompt: dict[str, Any]):  # type: ignore[no-untyped-def]
        captured.append(prompt)
        amount = "10%"
        return (
            {
                "headline": "" if len(captured) == 1 else f"Gain {amount}",
                "summary": "Markets move.",
                "numeric_facts": [amount],
            },
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
    assert audit.error_code == "summary_schema_invalid"
    failure = failures[0]
    assert isinstance(failure, ModelCallError)
    assert failure.validation_issues == ("headline:string_too_short",)
    assert audit.input_digest == "d" * 64
    assert "RETRY_GUIDANCE" not in captured[0]
    assert "exact OUTPUT_CONTRACT" in captured[1]["RETRY_GUIDANCE"]


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
    assert "distinct new events" in prompt["REFILL_GUIDANCE"]
    assert "put at most one in reserves" in prompt["REFILL_GUIDANCE"]
    assert "reuse that event's exact event_key" in prompt["REFILL_GUIDANCE"]
    assert "relevance" in prompt["REFILL_GUIDANCE"]
    assert "same absolute scale" in prompt["REFILL_GUIDANCE"]
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
        assert "empty quota is always better" in focus
        assert "Aim for at least 5 4-star stories" in focus
        assert "hard gate" in prompt["task"]
        contract = prompt["OUTPUT_CONTRACT"]
        assert contract["market"] == MARKET_VALUES
        assert f"publishes only selections tagged '{tag}'" in contract["market_rule"]
        assert contract["example"]["selections"][0]["market"] == tag


async def test_us_equity_prompt_prioritizes_equity_catalysts_and_primary_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daily_insights_api.modules.news.editions import US_EQUITY_SPEC

    client = DeepSeekClient(base_url="https://api.deepseek.com", api_key="secret", model="test")
    complete = AsyncMock(return_value=({"selections": []}, None, None, None, 1, "a" * 64))
    monkeypatch.setattr(client, "_complete", complete)

    await client.select([], policy=US_EQUITY_SPEC.selection)

    prompt = complete.call_args.args[0]
    focus = str(prompt["MARKET_FOCUS"])
    importance = str(prompt["OUTPUT_CONTRACT"]["importance"])
    assert "pricing of broad US indexes" in focus
    assert "30-40% market-wide drivers" in focus
    assert "60-70% company or sector equity catalysts" in focus
    assert "Evaluate event certainty" in focus
    assert "prefer the primary event" in focus
    assert "freshness penalty" in focus
    assert "24 hours old" in focus
    assert "Routine financing" in importance
    assert "at most 2" in importance
    assert "senior-note or bond issuance" in importance
    assert "investment-bank or CEO forecast" in importance


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
