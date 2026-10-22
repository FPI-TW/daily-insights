import uuid
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom import queue, scoring, triage
from daily_insights_api.modules.newsroom.clustering import CandidateEvent
from daily_insights_api.modules.newsroom.contracts import EventNew, TriageResult
from daily_insights_api.modules.newsroom.notifier import LogNotifier
from daily_insights_api.modules.newsroom.queue import RetryableStageError
from daily_insights_api.modules.newsroom.variants import BASELINE, P0, SelectionVariant
from daily_insights_api.modules.newsroom.worker import Runtime


def _result(
    event: object,
    *,
    story_type: str = "official",
    impact: int = 3,
    new_facts: bool = True,
    directness: tuple[int, int, int] = (4, 1, 3),
) -> TriageResult:
    return TriageResult.model_validate(
        {
            "topic": "policy",
            "story_type": story_type,
            "new_facts": new_facts,
            "impact": impact,
            "directness": dict(zip(("global", "tw_equity", "us_equity"), directness, strict=True)),
            "event": event,
        }
    )


def _candidate(title: str = "Fed holds rates") -> CandidateEvent:
    return CandidateEvent(id=uuid.uuid4(), working_title=title, similarity=0.9, titles=("A", "B"))


@pytest.mark.parametrize(("variant", "label"), [(BASELINE, "triage-v2"), (P0, "triage-v3")])
def test_prompt_is_versioned_by_its_content(variant: SelectionVariant, label: str) -> None:
    prompt = triage.load_triage_prompt(variant=variant)
    assert prompt.version.startswith(f"{label}:")
    assert len(prompt.version) == len(f"{label}:") + 12
    # JSON mode providers require the word in the prompt; the keys must match the contract.
    for needle in (
        "JSON",
        "story_type",
        "new_facts",
        "impact",
        "directness",
        "global",
        "tw_equity",
        "us_equity",
        "match",
        "new",
    ):
        assert needle in prompt.text
    for story_type in scoring.STORY_TYPE_CAP:
        assert story_type in prompt.text


def test_each_variant_loads_its_own_triage_prompt() -> None:
    baseline = triage.load_triage_prompt(variant=BASELINE)
    p0 = triage.load_triage_prompt(variant=P0)
    assert baseline.text != p0.text
    assert baseline.version.split(":")[1] != p0.version.split(":")[1]
    # Only v3 asks for escalation; v2 is frozen as the baseline.
    assert "escalation" not in baseline.text
    for needle in ("escalation", "航道", "升級"):
        assert needle in p0.text
    assert triage.load_triage_prompt(variant=P0) is p0


def _answer(**extra: object) -> dict[str, object]:
    return {
        "story_type": "catalyst",
        "new_facts": True,
        "impact": 5,
        "directness": {"global": 4, "tw_equity": 3, "us_equity": 3},
        "topic": "commodities",
        "event": {"new": "航道遇襲"},
    } | extra


@pytest.mark.parametrize("escalation", [0, 1, 2])
def test_v3_answers_carry_escalation(escalation: int) -> None:
    assert TriageResult.model_validate(_answer(escalation=escalation)).escalation == escalation


def test_v2_answers_without_escalation_read_as_zero() -> None:
    assert TriageResult.model_validate(_answer()).escalation == 0


@pytest.mark.parametrize("escalation", [-1, 3, 1.5, None])
def test_escalation_outside_zero_to_two_is_a_schema_error(escalation: object) -> None:
    # The provider validates the parsed JSON this way and retries a ValidationError.
    with pytest.raises(ValidationError):
        TriageResult.model_validate(_answer(escalation=escalation))


def test_translation_prompt_is_versioned_and_asks_for_json() -> None:
    prompt = triage.load_embed_translate_prompt()
    assert prompt.version.startswith("embed-translate-v1:")
    for needle in ("JSON", "title", "summary", "繁體中文"):
        assert needle in prompt.text


@pytest.mark.parametrize(
    ("title", "translate"),
    [
        ("TSMC September sales hit another record", True),
        ("Fed holds rates 5.25%", True),
        ("台積電9月營收創新高", False),
        ("盤中速報 - Onto Innovation Inc.(ONTO-US)大跌5.02%", False),
        ("フォートリア、臨床薬理部門を買収", False),
    ],
)
def test_only_headlines_without_cjk_are_translated(title: str, translate: bool) -> None:
    assert triage.needs_translation(title) is translate


def test_embedding_input_joins_title_and_summary_and_truncates() -> None:
    assert triage.embedding_input("  Fed   holds ", "Rates\n unchanged ") == (
        "Fed holds\nRates unchanged"
    )
    assert triage.embedding_input("Title", None) == "Title"
    assert triage.embedding_input("   ", "  ") == ""
    long = triage.embedding_input("T", "x" * 5_000)
    assert len(long) == triage.EMBED_INPUT_MAX_CHARS


def test_payload_carries_candidates_and_body_only_when_given() -> None:
    candidate = _candidate()
    payload = triage.triage_payload(
        title="Fed holds rates",
        source_name="Reuters",
        trust_tier=3,
        summary="s" * 3_000,
        body_excerpt=None,
        candidates=[candidate],
    )
    assert payload["article"]["source"] == {"name": "Reuters", "trust_tier": 3}
    assert len(payload["article"]["summary"]) == triage.SUMMARY_MAX_CHARS
    assert "body_excerpt" not in payload["article"]
    assert payload["candidate_events"] == [
        {"id": str(candidate.id), "working_title": "Fed holds rates", "titles": ["A", "B"]}
    ]
    with_body = triage.triage_payload(
        title="t",
        source_name="s",
        trust_tier=1,
        summary=None,
        body_excerpt="b" * 4_000,
        candidates=[],
    )
    assert len(with_body["article"]["body_excerpt"]) == triage.BODY_EXCERPT_CHARS
    assert with_body["candidate_events"] == []


def test_scores_follow_impact_and_directness() -> None:
    assert scoring.market_scores(
        _result(None, impact=5, directness=(4, 3, 2)), variant=BASELINE
    ) == {
        "global": 96,
        "tw_equity": 82,
        "us_equity": 67,
    }
    assert scoring.market_scores(
        _result(None, impact=1, directness=(4, 0, 1)), variant=BASELINE
    ) == {
        "global": 40,
        "tw_equity": 0,
        "us_equity": 14,
    }


def test_a_weak_link_alone_never_makes_an_article_relevant() -> None:
    for impact in (1, 2, 3, 4, 5):
        scores = scoring.market_scores(
            _result(None, impact=impact, directness=(1, 1, 0)), variant=BASELINE
        )
        assert not scoring.is_relevant(scores)
    assert scoring.is_relevant(
        scoring.market_scores(_result(None, impact=1, directness=(4, 0, 0)), variant=BASELINE)
    )


def test_no_new_facts_lowers_every_market() -> None:
    fresh = scoring.market_scores(_result(None, impact=3, directness=(4, 3, 0)), variant=BASELINE)
    rehash = scoring.market_scores(
        _result(None, impact=3, directness=(4, 3, 0), new_facts=False), variant=BASELINE
    )
    assert rehash == {
        "global": fresh["global"] - 15,
        "tw_equity": fresh["tw_equity"] - 15,
        "us_equity": 0,
    }


@pytest.mark.parametrize(
    ("story_type", "cap"),
    [("price_action", 40), ("preview", 55), ("opinion", 60), ("press_release", 45)],
)
def test_story_type_caps_are_applied_by_code(story_type: str, cap: int) -> None:
    scores = scoring.market_scores(_result(None, story_type=story_type, impact=5), variant=BASELINE)
    assert scores["global"] == cap
    assert max(scores.values()) == cap


def test_triage_signals_keep_the_raw_judgements() -> None:
    assert scoring.triage_signals(_result(None, impact=2, new_facts=False)) == {
        "impact": 2,
        "new_facts": False,
        "directness": {"global": 4, "tw_equity": 1, "us_equity": 3},
        "escalation": 0,
    }


def test_published_before_window_allows_two_hours_of_slack() -> None:
    edition = date(2026, 10, 8)
    # The window opens 2026-10-07 08:00 Taipei, 00:00 UTC.
    assert not triage.published_before_window(None, edition)
    assert not triage.published_before_window(datetime(2026, 10, 6, 22, 0, tzinfo=UTC), edition)
    assert triage.published_before_window(datetime(2026, 10, 6, 21, 59, tzinfo=UTC), edition)
    assert not triage.published_before_window(datetime(2026, 10, 7, 9, 0, tzinfo=UTC), edition)


def test_irrelevant_article_never_joins_an_event() -> None:
    candidate = _candidate()
    assert triage.resolve_event_choice(_result({"new": "x"}), [candidate], relevant=False) is None
    assert triage.resolve_event_choice(_result(None), [candidate], relevant=False) is None


def test_event_choice_accepts_new_or_an_offered_candidate() -> None:
    candidate = _candidate()
    created = triage.resolve_event_choice(
        _result({"new": "央行按兵不動"}), [candidate], relevant=True
    )
    assert created == EventNew(new="央行按兵不動")
    matched = triage.resolve_event_choice(
        _result({"match": f" {str(candidate.id).upper()} "}), [candidate], relevant=True
    )
    assert matched == candidate.id


@pytest.mark.parametrize("event", [{"match": str(uuid.uuid4())}, {"match": "e1"}, None])
def test_unknown_match_or_missing_event_is_a_schema_error(event: object) -> None:
    candidate = _candidate()
    with pytest.raises(RetryableStageError) as raised:
        triage.resolve_event_choice(_result(event), [candidate], relevant=True)
    assert raised.value.code == "triage_schema_invalid"


def test_register_binds_embed_and_fetch_gated_triage() -> None:
    runtime = Runtime(
        settings=Settings(),
        session_factory=async_sessionmaker(),
        llm=object(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        notifier=LogNotifier(),
    )
    registration = triage.register(runtime)
    bindings = {binding.stage.name: binding for binding in registration.stages}
    assert set(bindings) == {"embed", "triage"}
    assert bindings["embed"].stage is queue.EMBED
    assert bindings["embed"].concurrency == 2
    assert bindings["triage"].stage is queue.TRIAGE
    assert bindings["triage"].concurrency == 4
    assert bindings["triage"].extra_filter is triage.TRIAGE_READY
    assert registration.periodic == []
