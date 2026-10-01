import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom import queue, triage
from daily_insights_api.modules.newsroom.clustering import CandidateEvent
from daily_insights_api.modules.newsroom.contracts import EventNew, TriageResult
from daily_insights_api.modules.newsroom.notifier import LogNotifier
from daily_insights_api.modules.newsroom.queue import RetryableStageError
from daily_insights_api.modules.newsroom.worker import Runtime

SCORES = {"global": 80, "tw_equity": 20, "us_equity": 60}


def _result(relevant: bool, event: object) -> TriageResult:
    return TriageResult.model_validate(
        {"relevant": relevant, "topic": "policy", "market_scores": SCORES, "event": event}
    )


def _candidate(title: str = "Fed holds rates") -> CandidateEvent:
    return CandidateEvent(id=uuid.uuid4(), working_title=title, similarity=0.9, titles=("A", "B"))


def test_prompt_is_versioned_by_its_content() -> None:
    prompt = triage.load_triage_prompt()
    assert prompt.version.startswith("triage-v1:")
    assert len(prompt.version) == len("triage-v1:") + 12
    # JSON mode providers require the word in the prompt; the keys must match the contract.
    for needle in ("JSON", "market_scores", "global", "tw_equity", "us_equity", "match", "new"):
        assert needle in prompt.text


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


def test_irrelevant_article_never_joins_an_event() -> None:
    candidate = _candidate()
    assert triage.resolve_event_choice(_result(False, {"new": "x"}), [candidate]) is None
    assert triage.resolve_event_choice(_result(False, None), [candidate]) is None


def test_event_choice_accepts_new_or_an_offered_candidate() -> None:
    candidate = _candidate()
    created = triage.resolve_event_choice(_result(True, {"new": "央行按兵不動"}), [candidate])
    assert created == EventNew(new="央行按兵不動")
    matched = triage.resolve_event_choice(
        _result(True, {"match": f" {str(candidate.id).upper()} "}), [candidate]
    )
    assert matched == candidate.id


@pytest.mark.parametrize("event", [{"match": str(uuid.uuid4())}, {"match": "e1"}, None])
def test_unknown_match_or_missing_event_is_a_schema_error(event: object) -> None:
    candidate = _candidate()
    with pytest.raises(RetryableStageError) as raised:
        triage.resolve_event_choice(_result(True, event), [candidate])
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
