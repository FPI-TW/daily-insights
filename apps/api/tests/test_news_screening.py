from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import daily_insights_api.modules.news.llm as news_llm
from daily_insights_api.modules.news.contracts import Candidate
from daily_insights_api.modules.news.editions import GLOBAL_SPEC, TW_EQUITY_SPEC, US_EQUITY_SPEC
from daily_insights_api.modules.news.extraction import FetchedCandidate
from daily_insights_api.modules.news.failures import (
    FailureAction,
    NewsFailure,
    NewsOperationError,
    classify_failure,
)
from daily_insights_api.modules.news.llm import (
    SCREEN_MAX_HEADLINES,
    DeepSeekClient,
    HeadlineScreen,
    ModelCallError,
    ScreenedHeadline,
)
from daily_insights_api.modules.news.prompts import (
    SelectionCriteriaError,
    load_screen_criteria,
)
from daily_insights_api.modules.news.screening import (
    SCREEN_MAX_POOL,
    limit_screened_candidates,
    merge_screen_batches,
    order_pool,
    screen_batches,
    screen_failure_is_local,
    screen_input_digest,
    shortlist_limit,
)

NOW = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)


def _candidate(index: int, *, host: str | None = None, headline: str | None = None) -> Candidate:
    hostname = host or f"source{index}.example"
    return Candidate(
        id=f"{index:064x}",
        url=f"https://{hostname}/story-{index}",
        hostname=hostname,
        source_name=f"Source {index}",
        headline=headline or f"Story {index}",
        seen_at=NOW - timedelta(minutes=index),
    )


def _client() -> DeepSeekClient:
    return DeepSeekClient(base_url="https://api.deepseek.com", api_key="secret", model="m")


def _completion(payload: dict[str, Any], captured: list[dict[str, Any]]) -> Any:
    async def complete(
        prompt: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None, int | None, int | None, int, str]:
        captured.append(prompt)
        return (payload, "request", 10, 5, 3, "a" * 64)

    return complete


def test_packaged_screen_criteria_is_versioned_by_digest(tmp_path: Path) -> None:
    criteria = load_screen_criteria()
    assert "MARKET_FOCUS" in criteria.text
    assert "IMPORTANCE_SCALE" in criteria.text
    assert criteria.version == f"screen-v1:{criteria.digest[:12]}"
    with pytest.raises(SelectionCriteriaError, match="screen criteria resource is unavailable"):
        load_screen_criteria(tmp_path / "missing.txt")
    empty = tmp_path / "screen.txt"
    empty.write_text("  ", encoding="utf-8")
    with pytest.raises(SelectionCriteriaError, match="screen criteria must not be empty"):
        load_screen_criteria(empty)


def test_shortlist_limits_are_one_and_a_half_times_post_extraction_retention() -> None:
    assert shortlist_limit(GLOBAL_SPEC) == 60
    assert shortlist_limit(TW_EQUITY_SPEC) == 90
    assert shortlist_limit(US_EQUITY_SPEC) == 90
    assert SCREEN_MAX_POOL == 2 * SCREEN_MAX_HEADLINES == 400


async def test_screen_prompt_carries_headlines_market_focus_and_importance_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        client,
        "_complete",
        _completion({"shortlist": [{"n": 2, "score": 5}, {"n": 1, "score": 3}]}, captured),
    )
    headlines = [(1, _candidate(1)), (2, _candidate(2, headline="央行宣布降息"))]

    result = await client.screen(
        headlines, policy=TW_EQUITY_SPEC.selection, limit=90, shortlisted=("Earlier pick",)
    )

    assert result.value == HeadlineScreen(
        shortlist=(ScreenedHeadline(n=2, score=5), ScreenedHeadline(n=1, score=3))
    )
    assert (result.request_id, result.input_tokens, result.output_tokens) == ("request", 10, 5)
    prompt = captured[0]
    assert prompt["HEADLINES"] == [
        {
            "n": 1,
            "headline": "Story 1",
            "source": "Source 1",
            "published": "2026-09-29T23:59Z",
        },
        {
            "n": 2,
            "headline": "央行宣布降息",
            "source": "Source 2",
            "published": "2026-09-29T23:58Z",
        },
    ]
    assert prompt["MARKET_FOCUS"] == TW_EQUITY_SPEC.selection.market_focus
    assert prompt["IMPORTANCE_SCALE"] == TW_EQUITY_SPEC.selection.importance_guidance
    assert prompt["ALREADY_SHORTLISTED"] == ["Earlier pick"]
    assert "RETRY_GUIDANCE" not in prompt
    assert "0 to 90" in prompt["OUTPUT_CONTRACT"]["shortlist"]
    # Only headline metadata reaches the model; ids and URLs never do.
    assert "url" not in prompt["HEADLINES"][0]
    assert "id" not in prompt["HEADLINES"][0]


async def test_screen_discards_unknown_and_duplicate_numbers_and_honours_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        news_llm, "emit_event", lambda name, **fields: events.append((name, fields))
    )
    monkeypatch.setattr(
        client,
        "_complete",
        _completion(
            {
                "shortlist": [
                    {"n": 7, "score": 5},
                    {"n": 1, "score": 4},
                    {"n": 1, "score": 4},
                    {"n": 2, "score": 3},
                    {"n": 3, "score": 2},
                ]
            },
            [],
        ),
    )

    result = await client.screen([(number, _candidate(number)) for number in (1, 2, 3)], limit=2)

    assert isinstance(result.value, HeadlineScreen)
    assert [item.n for item in result.value.shortlist] == [1, 2]
    assert events == [("news.screen.discarded", {"unknown": 1, "duplicates": 1})]


@pytest.mark.parametrize(
    "payload",
    [
        {"shortlist": [{"n": 1, "score": 6}]},
        {"shortlist": [{"n": 1}]},
        {"shortlist": [{"n": 1, "score": 3, "reason": "extra"}]},
        {"ranked": []},
    ],
)
async def test_screen_schema_failure_is_repairable(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    client = _client()
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(client, "_complete", _completion(payload, captured))

    with pytest.raises(ModelCallError) as caught:
        await client.screen([(1, _candidate(1))], limit=5)

    assert caught.value.error_code == "screen_schema_invalid"
    assert caught.value.validation_issues
    failure = classify_failure(caught.value, stage="selection")
    assert (failure.code, failure.action) == ("screen_schema_invalid", "repair")

    await_retry: list[dict[str, Any]] = []
    monkeypatch.setattr(
        client, "_complete", _completion({"shortlist": [{"n": 1, "score": 3}]}, await_retry)
    )
    repaired = await client.screen(
        [(1, _candidate(1))], limit=5, retry_feedback="screen_schema_invalid"
    )
    assert isinstance(repaired.value, HeadlineScreen)
    assert "RETRY_GUIDANCE" in await_retry[0]
    # Fixed guidance only: the failure code itself never enters the prompt.
    assert "screen_schema_invalid" not in str(await_retry[0]["RETRY_GUIDANCE"])


async def test_screen_rejects_oversized_batches() -> None:
    headlines = [(number, _candidate(number)) for number in range(1, SCREEN_MAX_HEADLINES + 2)]
    with pytest.raises(ValueError, match="at most 200"):
        await _client().screen(headlines, limit=60)


def test_pool_ordering_and_batching_follow_the_pre_screen_ranking() -> None:
    plain = [_candidate(index) for index in range(3)]
    impact = _candidate(9, headline="Fed rate decision surprises markets")
    ordered = order_pool([*plain, impact], GLOBAL_SPEC.headline_impact_patterns)
    assert ordered[0] == impact
    assert ordered[1:] == plain

    pool = [_candidate(index) for index in range(450)]
    batches = screen_batches(pool[:SCREEN_MAX_POOL])
    assert [len(batch) for batch in batches] == [200, 200]
    assert batches[1][0] == (201, pool[200])


def test_merge_orders_by_score_then_call_order_and_limits() -> None:
    first = [(1, _candidate(1)), (2, _candidate(2)), (3, _candidate(3))]
    second = [(4, _candidate(4)), (5, _candidate(5))]
    picks = merge_screen_batches(
        [
            (
                first,
                HeadlineScreen(
                    shortlist=(
                        ScreenedHeadline(n=2, score=3),
                        ScreenedHeadline(n=1, score=4),
                    )
                ),
            ),
            (
                second,
                HeadlineScreen(
                    shortlist=(
                        ScreenedHeadline(n=5, score=4),
                        ScreenedHeadline(n=4, score=2),
                        # A number from another call is never resolved here.
                        ScreenedHeadline(n=3, score=5),
                    )
                ),
            ),
        ],
        limit=3,
    )
    assert [(pick.candidate.id, pick.rank, pick.score) for pick in picks] == [
        (_candidate(1).id, 1, 4),
        (_candidate(5).id, 2, 4),
        (_candidate(2).id, 3, 3),
    ]


def test_screened_limit_uses_rank_and_keeps_source_cap_and_total() -> None:
    candidates = [_candidate(index, host=f"host{index % 2}.example") for index in range(6)]
    fetched = [
        FetchedCandidate(candidate, str(candidate.url), "body", f"{index:064x}")
        for index, candidate in enumerate(candidates)
    ]
    picks = merge_screen_batches(
        [
            (
                list(enumerate(candidates, start=1)),
                HeadlineScreen(
                    shortlist=tuple(
                        ScreenedHeadline(n=number, score=3) for number in (6, 4, 2, 1, 3, 5)
                    )
                ),
            )
        ],
        limit=6,
    )
    ranks = {pick.candidate.id: pick for pick in picks}

    limited = limit_screened_candidates(fetched, ranks, total=3, per_source=2)

    # Rank order is 5 (host1), 3 (host1), 1 (host1, over the cap), 0 (host0).
    assert [item.candidate.id for item in limited] == [
        candidates[5].id,
        candidates[3].id,
        candidates[0].id,
    ]


def test_screen_digest_changes_only_with_the_screen_prompt() -> None:
    base = "b" * 64
    assert screen_input_digest(base, "c" * 64, "screen-v1:cccccccccccc") != base
    assert screen_input_digest(base, "c" * 64, "screen-v1:cccccccccccc") == screen_input_digest(
        base, "c" * 64, "screen-v1:cccccccccccc"
    )
    assert screen_input_digest(base, "c" * 64, "screen-v1:cccccccccccc") != screen_input_digest(
        base, "d" * 64, "screen-v1:dddddddddddd"
    )


@pytest.mark.parametrize(
    ("code", "action", "local"),
    [
        ("screen_schema_invalid_exhausted", "attention", True),
        ("provider_invalid_json_exhausted", "attention", True),
        ("provider_http_429", "retry", False),
        ("provider_http_401", "block", False),
        ("provider_http_429_repair_exhausted", "attention", False),
        ("unexpected_error", "attention", False),
    ],
)
def test_only_exhausted_validation_failures_fall_back(
    code: str, action: FailureAction, local: bool
) -> None:
    error = NewsOperationError(NewsFailure(code=code, action=action, stage="selection"))
    assert screen_failure_is_local(error) is local
