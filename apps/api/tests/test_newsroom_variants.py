import hashlib
from datetime import date

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.assembly import dedupe_prompt, editor_prompt_version
from daily_insights_api.modules.newsroom.contracts import EditorResult, TriageResult
from daily_insights_api.modules.newsroom.triage import load_triage_prompt
from daily_insights_api.modules.newsroom.variants import BASELINE, P0, VARIANTS, get_variant


def test_baseline_freezes_the_current_constants() -> None:
    # The eb6ad34 values: IMPACT_BASE and +5 per extra outlet, capped at +20.
    assert dict(BASELINE.impact_base) == {1: 40, 2: 54, 3: 68, 4: 82, 5: 96}
    assert BASELINE.source_bonus_mode == "linear"
    assert set(BASELINE.source_bonus_step.values()) == {5.0}
    assert set(BASELINE.source_bonus_cap.values()) == {20.0}
    assert BASELINE.escalation_waives_no_new_facts is False
    assert BASELINE.weight_clamp is None
    assert BASELINE.five_star_soft_cap is None


def test_variants_cover_every_market_and_impact() -> None:
    for variant in VARIANTS.values():
        assert get_variant(variant.name) is variant
        assert set(variant.impact_base) == {1, 2, 3, 4, 5}
        assert set(variant.source_bonus_step) == {"global", "tw_equity", "us_equity"}
        assert set(variant.source_bonus_cap) == {"global", "tw_equity", "us_equity"}
    assert P0.source_bonus_mode == "log2"


def test_settings_default_to_baseline() -> None:
    assert Settings(_env_file=None).newsroom_selection_variant == "baseline"


def test_p0b_variants_differ_only_where_intended() -> None:
    p0b = get_variant("p0b")
    assert (p0b.triage_prompt, p0b.editor_prompt) == ("triage_p0b", "editor_p0b")
    assert p0b.impact_base == P0.impact_base
    oldnews = get_variant("oldnews")
    assert oldnews.event_date_rule and oldnews.repeat_lookback_days == 14
    assert oldnews.triage_prompt == "triage_p0b" and not oldnews.earnings_merge_path
    earnings = get_variant("earnings_merge")
    assert earnings.earnings_merge_path and not earnings.event_date_rule
    assert earnings.dedupe_prompt_addenda == ("dedupe_addendum_earnings",)
    for variant in (BASELINE, P0, p0b):
        assert variant.triage_prompt_addenda == ()
        assert variant.repeat_lookback_days == 2 and variant.repeat_nearest_limit is None


def test_every_variant_prompt_loads() -> None:
    for variant in VARIANTS.values():
        prompt = load_triage_prompt(variant=variant)
        assert prompt.version.split(":")[0].split("+")[1:] == list(variant.triage_prompt_addenda)
        text, version = dedupe_prompt(variant=variant)
        assert text and version.startswith("newsroom.dedupe.v2")
        if variant.dedupe_prompt_addenda:
            assert version.rsplit(":", 1)[1] == hashlib.sha256(text.encode()).hexdigest()[:12]
        else:
            assert version == "newsroom.dedupe.v2"
        assert editor_prompt_version(variant=variant)


def test_event_date_is_lenient() -> None:
    base = {
        "story_type": "data",
        "new_facts": True,
        "impact": 4,
        "directness": {"global": 4, "tw_equity": 2, "us_equity": 3},
        "topic": "economy",
        "event": None,
    }
    assert TriageResult.model_validate(base).event_date is None
    assert TriageResult.model_validate({**base, "event_date": "2026-10-02"}).event_date == date(
        2026, 10, 2
    )
    for value in (None, "", "10/02", "unknown", 20261002):
        assert TriageResult.model_validate({**base, "event_date": value}).event_date is None


def test_older_model_answers_still_validate() -> None:
    triage = TriageResult.model_validate(
        {
            "story_type": "data",
            "new_facts": True,
            "impact": 4,
            "directness": {"global": 4, "tw_equity": 2, "us_equity": 3},
            "topic": "economy",
            "event": None,
        }
    )
    assert triage.escalation == 0
    editor = EditorResult.model_validate({"ratings": [{"event_id": "e1", "stars": 4}]})
    assert editor.ratings[0].reason == ""


def test_editor_reason_never_fails_the_call() -> None:
    result = EditorResult.model_validate(
        {
            "ratings": [
                {"event_id": "e1", "stars": 2, "reason": None},
                {"event_id": "e2", "stars": 5, "reason": "  " + "很" * 300},
            ]
        }
    )
    assert result.ratings[0].reason == ""
    assert len(result.ratings[1].reason) == 200
