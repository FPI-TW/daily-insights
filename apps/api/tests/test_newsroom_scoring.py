"""Selection-variant scoring: triage market scores and the assembly event score.

``BASELINE`` must reproduce the eb6ad34 formula exactly; ``P0`` is the 2026-10-22
recalibration (variants.py).
"""

import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from daily_insights_api.modules.newsroom import scoring
from daily_insights_api.modules.newsroom.assembly import (
    ArticleSignal,
    _ArticleRow,
    clamp_weight,
    distinct_outlets,
    event_score,
    rank_candidates,
    source_bonus,
)
from daily_insights_api.modules.newsroom.contracts import TriageResult
from daily_insights_api.modules.newsroom.variants import BASELINE, P0, SelectionVariant


def _triage(
    *, impact: int = 4, new_facts: bool = True, escalation: int = 0, story_type: str = "official"
) -> TriageResult:
    return TriageResult.model_validate(
        {
            "topic": "commodities",
            "story_type": story_type,
            "new_facts": new_facts,
            "impact": impact,
            "directness": {"global": 4, "tw_equity": 2, "us_equity": 3},
            "escalation": escalation,
            "event": None,
        }
    )


# --- Triage market scores ---------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "expected"),
    [(BASELINE, [40, 54, 68, 82, 96]), (P0, [30, 45, 62, 80, 100])],
)
def test_impact_base_follows_the_variant(variant: SelectionVariant, expected: list[int]) -> None:
    scores = [
        scoring.market_scores(_triage(impact=impact), variant=variant)["global"]
        for impact in (1, 2, 3, 4, 5)
    ]
    assert scores == expected


def test_directness_still_scales_the_new_impact_base() -> None:
    assert scoring.market_scores(_triage(impact=5), variant=P0) == {
        "global": 100,
        "tw_equity": 70,
        "us_equity": 85,
    }
    assert scoring.market_scores(_triage(impact=4), variant=P0) == {
        "global": 80,
        "tw_equity": 56,
        "us_equity": 68,
    }


def test_clear_escalation_waives_the_no_new_facts_penalty_in_p0() -> None:
    fresh = scoring.market_scores(_triage(), variant=P0)
    escalated = scoring.market_scores(_triage(new_facts=False, escalation=2), variant=P0)
    continued = scoring.market_scores(_triage(new_facts=False, escalation=1), variant=P0)
    assert escalated == fresh
    assert continued == {market: score - 15 for market, score in fresh.items()}


def test_baseline_always_applies_the_no_new_facts_penalty() -> None:
    fresh = scoring.market_scores(_triage(), variant=BASELINE)
    for escalation in (0, 1, 2):
        rehash = scoring.market_scores(
            _triage(new_facts=False, escalation=escalation), variant=BASELINE
        )
        assert rehash == {market: score - 15 for market, score in fresh.items()}


def test_story_type_cap_still_holds_after_an_escalation_waiver() -> None:
    scores = scoring.market_scores(
        _triage(impact=5, new_facts=False, escalation=2, story_type="preview"), variant=P0
    )
    assert scores["global"] == scoring.STORY_TYPE_CAP["preview"]


def test_relevance_bar_is_unchanged() -> None:
    assert scoring.RELEVANT_SCORE == 40
    # Impact 2 at full directness falls from 54 to 45: still relevant, by less.
    assert scoring.is_relevant(scoring.market_scores(_triage(impact=2), variant=P0))
    assert not scoring.is_relevant(scoring.market_scores(_triage(impact=1), variant=P0))


def test_triage_signals_store_the_escalation() -> None:
    assert scoring.triage_signals(_triage(escalation=2))["escalation"] == 2
    older = TriageResult.model_validate(
        {
            "topic": "policy",
            "story_type": "official",
            "new_facts": True,
            "impact": 3,
            "directness": {"global": 4, "tw_equity": 0, "us_equity": 0},
            "event": None,
        }
    )
    assert scoring.triage_signals(older)["escalation"] == 0


# --- Multi-source bonus and weight clamp ------------------------------------------


@pytest.mark.parametrize(
    ("n", "global_bonus", "tw_bonus"),
    [
        (0, 0.0, 0.0),
        (1, 0.0, 0.0),
        (2, 6.0, 4.0),
        (3, 6 * math.log2(3), 4 * math.log2(3)),
        # 6 x log2(6) = 15.5 and 4 x log2(6) = 10.3: both just past their caps.
        (6, 15.0, 10.0),
        (40, 15.0, 10.0),
    ],
)
def test_p0_source_bonus_is_logarithmic_per_market(
    n: int, global_bonus: float, tw_bonus: float
) -> None:
    assert source_bonus(n, "global", variant=P0) == pytest.approx(global_bonus)
    assert source_bonus(n, "us_equity", variant=P0) == pytest.approx(global_bonus)
    assert source_bonus(n, "tw_equity", variant=P0) == pytest.approx(tw_bonus)


@pytest.mark.parametrize(("n", "bonus"), [(1, 0.0), (2, 5.0), (3, 10.0), (5, 20.0), (9, 20.0)])
def test_baseline_source_bonus_is_five_per_extra_outlet(n: int, bonus: float) -> None:
    for market in ("global", "tw_equity", "us_equity"):
        assert source_bonus(n, market, variant=BASELINE) == bonus


def test_outlets_are_counted_by_publisher() -> None:
    signals = [
        ArticleSignal("CNBC", 50, 1.0),
        ArticleSignal("CNBC", 60, 1.0),
        ArticleSignal("Reuters", 40, 1.0),
    ]
    assert distinct_outlets(signals) == 2
    assert distinct_outlets([]) == 0


@pytest.mark.parametrize(("weight", "clamped"), [(0.5, 0.9), (0.8, 0.9), (1.0, 1.0), (1.2, 1.1)])
def test_p0_clamps_source_weight(weight: float, clamped: float) -> None:
    assert clamp_weight(weight, variant=P0) == clamped
    assert clamp_weight(weight, variant=BASELINE) == weight


def test_p0_event_score_clamps_weight_before_the_story_type_cap() -> None:
    assert event_score([ArticleSignal("CNA", 82, 1.2)], "global", variant=P0) == pytest.approx(90.2)
    assert event_score([ArticleSignal("Blog", 50, 0.5)], "global", variant=P0) == 45.0
    # An opinion piece at its 60 ceiling stays there from a weighted source.
    assert event_score([ArticleSignal("CNA", 60, 1.2, cap=60)], "global", variant=P0) == 60.0


def test_p0_event_score_adds_the_market_bonus() -> None:
    signals = [ArticleSignal(f"outlet-{index}", 60, 1.0) for index in range(4)]
    assert event_score(signals, "global", variant=P0) == 72.0
    assert event_score(signals, "tw_equity", variant=P0) == 68.0
    assert event_score(signals, "global", variant=BASELINE) == 75.0


# --- Candidate ranking ------------------------------------------------------------


START = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)


def _row(
    event_id: uuid.UUID,
    outlet: str,
    scores: dict[str, int],
    *,
    weight: float = 1.0,
    story_type: str = "official",
    has_body: bool = True,
    minutes: int = 0,
) -> _ArticleRow:
    return _ArticleRow(
        event_id=event_id,
        title=f"{outlet} report",
        source_id=uuid.uuid5(uuid.NAMESPACE_URL, outlet),
        source_name=outlet,
        weight=weight,
        trust_tier=2,
        market_scores=dict(scores),
        has_body=has_body,
        story_type=story_type,
        first_seen_at=START + timedelta(minutes=minutes),
    )


def _fixture() -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, list[_ArticleRow]]]:
    """A running crisis from weighted wires, a small company widely rewritten, and noise."""
    ids = {name: uuid.uuid5(uuid.NAMESPACE_URL, name) for name in ("crisis", "company", "opinion")}
    ids |= {name: uuid.uuid5(uuid.NAMESPACE_URL, name) for name in ("moves", "no-body")}
    crisis = {"global": 82, "tw_equity": 57, "us_equity": 70}
    company = {"global": 0, "tw_equity": 68, "us_equity": 0}
    articles = {
        ids["crisis"]: [
            _row(ids["crisis"], "Reuters", crisis),
            _row(ids["crisis"], "CNA", crisis, weight=1.2, minutes=5),
            _row(ids["crisis"], "CNBC", crisis, weight=1.2, minutes=9),
        ],
        ids["company"]: [
            _row(ids["company"], f"tw-outlet-{index}", company, minutes=index) for index in range(6)
        ],
        ids["opinion"]: [
            _row(
                ids["opinion"],
                "CNA",
                {"global": 60, "tw_equity": 60, "us_equity": 0},
                weight=1.2,
                story_type="opinion",
            )
        ],
        # Price action alone never qualifies; neither does an event without a full text.
        ids["moves"]: [
            _row(ids["moves"], "UDN", {"global": 40, "tw_equity": 40}, story_type="price_action")
        ],
        ids["no-body"]: [_row(ids["no-body"], "UDN", company, has_body=False)],
    }
    return {event_id: name for name, event_id in ids.items()}, articles


def _ranked(market: str, variant: SelectionVariant) -> list[tuple[str, float]]:
    titles, articles = _fixture()
    return [
        (candidate.working_title, candidate.score)
        for candidate in rank_candidates(titles, articles, market, variant=variant)
    ]


def test_baseline_ranking_reproduces_the_eb6ad34_scores() -> None:
    assert _ranked("tw_equity", BASELINE) == [
        # 68 from six outlets, +5 for each of the five extra, capped at +20.
        ("company", 88.0),
        # 57 x 1.2 from the weighted wire, +10 for two extra outlets.
        ("crisis", pytest.approx(78.4)),
        ("opinion", 60.0),
    ]
    assert _ranked("global", BASELINE) == [
        ("crisis", pytest.approx(108.4)),
        ("opinion", 60.0),
    ]


def test_p0_ranking_narrows_the_taiwan_rewrite_bonus() -> None:
    assert _ranked("tw_equity", P0) == [
        ("company", 78.0),
        ("crisis", pytest.approx(57 * 1.1 + 4 * math.log2(3))),
        ("opinion", 60.0),
    ]
    assert _ranked("global", P0) == [
        ("crisis", pytest.approx(82 * 1.1 + 6 * math.log2(3))),
        ("opinion", 60.0),
    ]
