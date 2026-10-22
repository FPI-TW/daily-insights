from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.contracts import EditorResult, TriageResult
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


def test_settings_default_to_p0() -> None:
    assert Settings(_env_file=None).newsroom_selection_variant == "p0"


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
