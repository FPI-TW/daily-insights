import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom import triage
from daily_insights_api.modules.newsroom.models import NewsroomLlmCall
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.replay import agreement, costs, runner, snapshot
from daily_insights_api.modules.newsroom.replay import cache as cache_module
from daily_insights_api.modules.newsroom.replay.cache import ResponseCache, request_key
from daily_insights_api.modules.newsroom.replay.provider import LlmProvider, parse_extra_body
from daily_insights_api.modules.newsroom.variants import BASELINE
from daily_insights_api.scripts import run_newsroom_replay

GO = LlmProvider(
    base_url="https://go.test/v1",
    model="deepseek-v4.1-flash",
    key_file=Path("/nonexistent/key"),
    extra_body={"thinking": {"type": "disabled"}},
    session_header="x-opencode-session",
)
EVENT = str(uuid.UUID(int=99))


def _row(
    index: int, *, relevant: bool, topic: str = "markets", title: str = "台股"
) -> dict[str, Any]:
    return {
        "id": str(uuid.UUID(int=index)),
        "edition_date": "2026-10-22",
        "relevant": relevant,
        "topic": topic,
        "title": title,
    }


def _triage_answer(
    *, impact: int = 4, directness: int = 4, event: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "topic": "markets",
        "story_type": "data",
        "new_facts": True,
        "impact": impact,
        "directness": {"global": directness, "tw_equity": 0, "us_equity": 0},
        "event": event,
    }


def _sampled(index: int = 1, **changes: Any) -> agreement.Sampled:
    fields: dict[str, Any] = {
        "article_id": str(uuid.UUID(int=index)),
        "edition_date": "2026-10-22",
        "title": "Fed holds rates",
        "language": "other",
        "stratum": "s",
        "relevant": True,
        "topic": "markets",
        "story_type": "data",
        "signals": {
            "impact": 4,
            "new_facts": True,
            "directness": {"global": 4, "tw_equity": 0, "us_equity": 0},
            "escalation": 0,
        },
        "choice": f"match:{EVENT}",
        "input_tokens": 1000,
        "payload": {"article": {"title": "Fed holds rates"}, "candidate_events": []},
        "candidate_ids": [EVENT],
    }
    return agreement.Sampled(**(fields | changes))


# --- Provider --------------------------------------------------------------------------


def test_extra_body_must_be_an_object_without_the_clients_own_fields() -> None:
    assert parse_extra_body('{"thinking":{"type":"disabled"}}') == {
        "thinking": {"type": "disabled"}
    }
    assert parse_extra_body("") == {}
    for wrong in ('{"model":"x"}', '{"temperature":1}', "[1]", "{not json"):
        with pytest.raises(ValueError):
            parse_extra_body(wrong)


def test_the_provider_tag_separates_thinking_on_and_off_and_hides_the_key(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("sk-secret\n", encoding="utf-8")
    off = LlmProvider("https://go.test/v1", "m", key_file, extra_body={"thinking": {"type": "x"}})
    on = LlmProvider("https://go.test/v1/", "m", key_file)
    assert off.cache_tag() != on.cache_tag()
    assert off.read_key() == "sk-secret"
    described = json.dumps(off.describe())
    assert "sk-secret" not in described and str(key_file) not in described
    keyed = [
        request_key(model="m", system="s", payload={}, subject=None, provider=tag)[0]
        for tag in (None, off.cache_tag(), on.cache_tag())
    ]
    assert len(set(keyed)) == 3
    assert keyed[0] == request_key(model="m", system="s", payload={}, subject=None)[0]


def test_an_empty_or_missing_key_file_stops_the_run(tmp_path: Path) -> None:
    empty = tmp_path / "key"
    empty.write_text("\n", encoding="utf-8")
    for path in (empty, tmp_path / "missing"):
        with pytest.raises(snapshot.ReplayError):
            LlmProvider("https://go.test", "m", path).read_key()


def test_the_llm_options_go_together_and_reach_the_run() -> None:
    options = run_newsroom_replay.parse_args(
        [
            "--llm-base-url",
            GO.base_url,
            "--llm-model",
            GO.model,
            "--llm-key-file",
            "/tmp/key",
            "--llm-extra-body",
            '{"thinking":{"type":"disabled"}}',
            "--llm-session-header",
            "x-opencode-session",
            "run",
            "--stages",
            "editor",
        ]
    )
    assert options.provider == LlmProvider(
        GO.base_url,
        GO.model,
        Path("/tmp/key"),
        extra_body={"thinking": {"type": "disabled"}},
        session_header="x-opencode-session",
    )
    assert options.provider.user_agent == "daily-insights-newsroom-eval/1.0"
    assert run_newsroom_replay.parse_args(["run"]).provider is None
    for wrong in (
        ["--llm-base-url", GO.base_url, "run"],
        ["agreement-ask", "--name", "go", "--label", "A1", "--budget-usd", "1"],
        [
            *("--llm-base-url", "u", "--llm-model", "m", "--llm-key-file", "k"),
            *("--llm-extra-body", '{"model":"x"}', "run"),
        ],
    ):
        with pytest.raises(SystemExit):
            run_newsroom_replay.parse_args(wrong)


def test_a_provider_replaces_the_endpoint_models_and_extra_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(snapshot, "read_provider_setting", lambda *_: None)
    environment = snapshot.ReplayEnvironment(root=tmp_path)
    options = runner.RunOptions(
        snapshot=snapshot.Snapshot(datetime(2026, 10, 23).date(), datetime(2026, 10, 24).date()),
        variant=BASELINE,
        stages="editor",
        provider=GO,
    )
    settings = runner._settings(environment, options, "run_db")
    assert settings.newsroom_llm_base_url == GO.base_url
    assert settings.newsroom_triage_model == settings.newsroom_editor_model == GO.model
    assert settings.newsroom_llm_extra_body == {"thinking": {"type": "disabled"}}
    assert settings.newsroom_llm_session_header == "x-opencode-session"
    assert settings.newsroom_llm_api_key is None
    production = runner._settings(
        environment,
        runner.RunOptions(snapshot=options.snapshot, variant=BASELINE, stages="editor"),
        "run_db",
    )
    assert production.newsroom_triage_model == "deepseek-chat"
    assert production.newsroom_llm_extra_body == {}


# --- Ledger ----------------------------------------------------------------------------


def test_go_calls_are_recorded_at_their_list_cost(tmp_path: Path) -> None:
    ledger = costs.Ledger(tmp_path / "go.jsonl", "agreement-go-A1")
    entry = ledger.chat(
        stage="triage triage-v2:x",
        model="deepseek-v4.1-flash",
        usages=[
            {
                "prompt_tokens": 3500,
                "completion_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 2400},
                "completion_tokens_details": {"reasoning_tokens": 40},
                "cost": 0,
                "opencode": {"billing_source": "go", "list_cost": 0.00042},
                "model": "deepseek-v4.1-flash",
            }
        ],
    )
    assert entry is not None
    assert entry.usd == pytest.approx(0.00042) and entry.list_cost == pytest.approx(0.00042)
    assert (entry.cache_hit_tokens, entry.reasoning_tokens) == (2400, 40)
    [stored] = costs.read_ledger(ledger.path)
    assert stored == entry
    assert costs.ledger_total([stored])["reasoning_tokens"] == 40


def test_deepseek_usage_is_still_priced_from_its_token_split(tmp_path: Path) -> None:
    ledger = costs.Ledger(tmp_path / "p0b.jsonl", "run")
    entry = ledger.chat(
        stage="editor",
        model="deepseek-chat",
        usages=[{"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 60}],
    )
    assert entry is not None and entry.list_cost is None and entry.usd > 0
    # A ledger line written before Go existed has neither new field.
    old = {k: v for k, v in asdict(entry).items() if k not in {"list_cost", "reasoning_tokens"}}
    (tmp_path / "old.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
    assert costs.read_ledger(tmp_path / "old.jsonl")[0].list_cost is None


# --- Sample ----------------------------------------------------------------------------


def test_every_stratum_is_sampled_and_the_rest_follows_its_size() -> None:
    counts = agreement.allocate({"a": 100, "b": 10, "c": 1, "d": 0}, 20)
    assert counts == {"a": 16, "b": 3, "c": 1}
    assert sum(agreement.allocate({"a": 3, "b": 3}, 6).values()) == 6
    with pytest.raises(ValueError):
        agreement.allocate({"a": 1, "b": 1}, 1)


def test_a_draw_is_stratified_and_repeatable() -> None:
    rows = [_row(i, relevant=i % 2 == 0) for i in range(40)]
    rows += [_row(100 + i, relevant=True, title="Fed holds") for i in range(5)]
    first = agreement.draw(rows, 10)
    assert [row["id"] for row in first] == [row["id"] for row in agreement.draw(rows[::-1], 10)]
    assert {agreement.stratum(row) for row in first} == {agreement.stratum(row) for row in rows}
    assert len(first) == 10


def test_production_choice_is_read_back_from_event_stamps() -> None:
    called = datetime(2026, 10, 21, 3, tzinfo=UTC)
    later = datetime(2026, 10, 21, 4, tzinfo=UTC)
    base = {
        "relevant": True,
        "event_id": EVENT,
        "opened": False,
        "called_at": called,
        "event_created_at": datetime(2026, 10, 21, 2, tzinfo=UTC),
    }
    assert agreement.production_choice(base, [EVENT], {}) == f"match:{EVENT}"
    assert agreement.production_choice(base, [], {}) == "unknown"
    assert agreement.production_choice(base | {"opened": True}, [], {}) == "new"
    assert agreement.production_choice(base | {"event_created_at": later}, [], {}) == "new"
    assert agreement.production_choice(base | {"event_id": None}, [], {}) == "new"
    assert agreement.production_choice(base | {"relevant": False}, [EVENT], {}) is None
    assert agreement.production_choice(base, [EVENT], {"other": EVENT}) == "unknown"


def test_the_holdout_period_is_never_sampled() -> None:
    class Untouched:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("production was queried")

    with pytest.raises(snapshot.ReplayError):
        agreement.build_sample(
            Untouched(),  # type: ignore[arg-type]
            start=datetime(2026, 10, 12).date(),
            end=datetime(2026, 10, 14).date(),
            size=10,
        )


# --- Compare ---------------------------------------------------------------------------


def test_answers_are_scored_like_the_baseline_triage_handler() -> None:
    item = _sampled()
    matched = agreement.answer_judgement(
        item, agreement.Answer(result=_triage_answer(event={"match": EVENT.upper()}))
    )
    assert matched is not None and matched.relevant and matched.event == f"match:{EVENT}"
    elsewhere = agreement.answer_judgement(
        item, agreement.Answer(result=_triage_answer(event={"match": str(uuid.uuid4())}))
    )
    assert elsewhere is not None and elsewhere.event == "invalid"
    weak = agreement.answer_judgement(item, agreement.Answer(result=_triage_answer(directness=1)))
    assert weak is not None and not weak.relevant and weak.event is None
    assert agreement.answer_judgement(item, agreement.Answer(error="triage_schema_invalid")) is None


def test_pairs_count_each_field_and_skip_untraceable_events() -> None:
    sample = [_sampled(1), _sampled(2), _sampled(3, merge_affected=True)]
    production = agreement.production_side(sample).judgements
    answers = {
        sample[0].article_id: agreement.Answer(result=_triage_answer(event={"match": EVENT})),
        sample[1].article_id: agreement.Answer(result=_triage_answer(impact=3, event={"new": "x"})),
        sample[2].article_id: agreement.Answer(result=_triage_answer(event={"new": "x"})),
    }
    go = agreement.side("A1", sample, answers)
    stats = agreement.compare_pair(production, go.judgements, sample)
    assert stats.articles == 3
    assert (stats.same["impact"], stats.compared["impact"]) == (2, 3)
    assert (stats.same["event"], stats.compared["event"]) == (1, 2)
    assert stats.impact_within_one == 3 and stats.impact_shift == -1
    assert stats.any_difference == 1
    text = agreement.report(
        {"start": "2026-10-22", "end": "2026-10-22", "population": 3, "prompt_version": "v"},
        sample,
        {"A1": ({"rep": 1, "provider": GO.describe()}, answers)},
    )
    assert "正式 vs A1" in text and "2/3" in text


def test_redraw_noise_compares_identical_requests_pairwise() -> None:
    def rows(impact: int) -> list[dict[str, Any]]:
        new = {"impact": impact, "story_type": "data", "new_facts": True, "escalation": 0}
        return [
            {"step": "p0b", "article_id": "a", "edition_date": "2026-10-18", "new": new},
            {"step": "p0b", "article_id": "h", "edition_date": "2026-10-15", "new": new},
        ]

    noise = agreement.redraw_noise([rows(4), rows(4), rows(3)])
    assert [pair["articles"] for pair in noise["pairs"]] == [1, 1, 1]
    assert [pair["same"]["impact"] for pair in noise["pairs"]] == [1, 0, 0]


# --- Ask -------------------------------------------------------------------------------


class GoModel:
    """Answers like OpenCode Go: list cost and reasoning tokens in the usage."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete[R: BaseModel](
        self,
        database: AsyncSession,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        result_type: type[R],
        audit: CallAudit,
    ) -> R:
        self.calls += 1
        sink = cache_module._usage_sink.get()
        assert sink is not None
        sink.append(
            {
                "prompt_tokens": 3000,
                "completion_tokens": 80,
                "opencode": {"list_cost": 0.001},
                "model": model,
            }
        )
        database.add(
            NewsroomLlmCall(
                stage=audit.stage, model=model, input_tokens=3000, output_tokens=80, latency_ms=1
            )
        )
        return result_type.model_validate(_triage_answer(event={"new": "x"}))


async def test_asking_caches_under_the_provider_and_stops_at_the_budget(tmp_path: Path) -> None:
    sample = [_sampled(index) for index in range(1, 4)]
    cache = ResponseCache(tmp_path / "cache")
    ledger = costs.Ledger(tmp_path / "go.jsonl", "agreement-go-A1")
    live = GoModel()
    answers = await agreement.ask(
        sample, cache=cache, ledger=ledger, provider=GO, rep=1, budget_usd=1.0, live=lambda: live
    )
    assert live.calls == 3
    assert {answer.input_tokens for answer in answers.values()} == {3000}
    assert costs.ledger_total(costs.read_ledger(ledger.path))["usd"] == pytest.approx(0.003)
    again = await agreement.ask(
        sample, cache=cache, ledger=ledger, provider=GO, rep=1, budget_usd=1.0, live=lambda: live
    )
    assert live.calls == 3 and again.keys() == answers.keys()
    prompt = triage.load_triage_prompt(variant=BASELINE)
    plain_key, _ = request_key(
        model=GO.model,
        system=prompt.text,
        payload=sample[0].payload,
        subject=uuid.UUID(sample[0].article_id),
    )
    assert cache.answer(plain_key, 0) is None
    with pytest.raises(agreement.BudgetReached):
        await agreement.ask(
            sample,
            cache=cache,
            ledger=ledger,
            provider=GO,
            rep=2,
            budget_usd=0.003,
            live=lambda: live,
        )
    assert live.calls == 3


# --- Editor-only provider runs -----------------------------------------------------------


def test_only_the_named_stages_go_to_the_provider() -> None:
    options = run_newsroom_replay.parse_args(
        [
            *("--llm-base-url", GO.base_url, "--llm-model", GO.model, "--llm-key-file", "k"),
            *("--llm-stages", "editor", "--llm-timeout", "300"),
            *("--production-model", "deepseek-chat", "run", "--label", "A"),
        ]
    )
    assert options.provider.stages == ("editor",)
    assert options.provider.timeout_seconds == 300
    assert options.production_model == "deepseek-chat" and options.label == "A"
    for wrong in (["--llm-stages", "analysis"], ["--llm-timeout", "0"]):
        with pytest.raises(SystemExit):
            run_newsroom_replay.parse_args(
                [*("--llm-base-url", "u", "--llm-model", "m", "--llm-key-file", "k"), *wrong, "run"]
            )


def test_an_editor_only_provider_keeps_production_triage_from_the_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(snapshot, "read_provider_setting", lambda *_: "deepseek-v4.1-flash")
    environment = snapshot.ReplayEnvironment(root=tmp_path)
    options = runner.RunOptions(
        snapshot=snapshot.Snapshot(datetime(2026, 10, 18).date(), datetime(2026, 10, 22).date()),
        variant=BASELINE,
        stages="triage",
        provider=LlmProvider(
            GO.base_url, GO.model, GO.key_file, stages=("editor",), timeout_seconds=300
        ),
        production_model="deepseek-chat",
    )
    settings = runner._settings(environment, options, "run_db")
    assert settings.newsroom_triage_model == "deepseek-chat"
    assert settings.newsroom_editor_model == GO.model
    assert settings.newsroom_llm_timeout_seconds == 300
    runtime = runner._runtime(
        environment,
        options,
        settings,
        engine=None,  # type: ignore[arg-type]
        gate_holder=[None],
        stats=cache_module.CacheStats(),
        ledger=costs.Ledger(tmp_path / "go.jsonl", "run"),
        failures=cache_module.ApiFailures(),
    )
    router = runtime.llm
    assert isinstance(router, cache_module.StageRouter)
    production = router._default
    assert isinstance(production, cache_module.CachedJsonModel)
    assert production._live_factory is None and production.provider is None
    go = router._routes["editor"]
    assert isinstance(go, cache_module.CachedJsonModel) and options.provider is not None
    assert go.provider == options.provider.cache_tag()


def test_call_stats_show_latency_repairs_and_cost_per_condition(tmp_path: Path) -> None:
    ledger = costs.Ledger(tmp_path / "go.jsonl", "20261027-1205-baseline-triage-alt-A")
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "opencode": {"list_cost": 0.01}}
    ledger.chat(stage="editor", model="m", usages=[usage], latency_ms=1_000)
    ledger.chat(stage="editor", model="m", usages=[usage, usage], latency_ms=120_000)
    other = costs.Ledger(ledger.path, "20261027-1205-baseline-triage-alt-B")
    other.chat(stage="editor", model="m", usages=[usage], latency_ms=2_000, failed=True)
    text = costs.calls_markdown(costs.read_ledger(ledger.path), {"A": "-A", "B": "-B", "C": "-C"})
    rows = {line.split(" | ")[0].strip("| "): line for line in text.splitlines()[2:]}
    assert "| 2 | 1.0 s | 120.0 s | 120.0 s | 1 | 1 | 0 |" in rows["A"]
    assert "| 1 |" in rows["B"] and "| 0 | 0 | 1 |" in rows["B"]
    assert rows["C"].startswith("| C | 0 |")


def test_label_candidates_union_picks_and_the_top_by_score() -> None:
    from daily_insights_api.modules.newsroom.replay.metrics import EventRow, MarketDay
    from daily_insights_api.modules.newsroom.replay.multirun import Group, Run, label_candidates

    def day(stars: dict[str, int], picked: set[str]) -> MarketDay:
        rows = [
            EventRow(event_id=e, title=f"t{e}", position=i, score=1, stars=s, picked=e in picked)
            for i, (e, s) in enumerate(stars.items(), start=1)
        ]
        return MarketDay(
            edition_date="2026-10-18",
            market="global",
            selection_mode="editor",
            candidates=rows,
            picks=[row for row in rows if row.picked],
        )

    a = Group("A", [Run("a1", {}, [day({"x": 5, "y": 3, "z": 2}, {"x"})])])
    b = Group("B", [Run("b1", {}, [day({"x": 4, "y": 4, "z": 2}, {"x", "y"})])])
    text = label_candidates([a, b], top=1)
    assert "| 1 | tx | - | 5 (1/1) | 4 (1/1) |  |  |" in text
    assert "| 2 | ty | - | 3 (0/1) | 4 (1/1) |  |  |" in text
    assert "tz" not in text


def test_runs_started_in_the_same_minute_get_their_own_directories(tmp_path: Path) -> None:
    environment = snapshot.ReplayEnvironment(root=tmp_path)
    options = runner.RunOptions(
        snapshot=snapshot.Snapshot(datetime(2026, 10, 18).date(), datetime(2026, 10, 22).date()),
        variant=BASELINE,
        stages="triage",
        provider=GO,
        label="A",
    )
    now = datetime(2026, 10, 27, 4, tzinfo=UTC)
    first = runner._run_name(environment, options, now)
    second = runner._run_name(environment, options, now)
    assert first.name == "20261027-1200-baseline-triage-alt-A"
    assert second.name == "20261027-1200-baseline-triage-alt-A-2"
    assert first.is_dir() and second.is_dir()
