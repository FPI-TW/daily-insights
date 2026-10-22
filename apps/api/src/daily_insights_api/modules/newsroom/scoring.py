"""Article market scores computed from triage's discrete judgements (spec §6.2).

Triage answers closed questions (story type, impact, new facts, directness per
market); the 0-100 score each market edition ranks by is derived here, so the
caps for price-action, preview, opinion and press-release pieces are applied by
code instead of being left to the model. The impact scale and the escalation
waiver come from the selection variant (``variants.py``).
"""

from daily_insights_api.modules.newsroom.contracts import TriageResult
from daily_insights_api.modules.newsroom.variants import SelectionVariant

RELEVANT_SCORE = 40
# Share of the impact base reaching a market, by directness. Directness 1 always
# lands below ``RELEVANT_SCORE``, so a weak link alone never makes an article relevant.
DIRECTNESS_FACTOR = {0: 0.0, 1: 0.35, 2: 0.7, 3: 0.85, 4: 1.0}
NO_NEW_FACTS_PENALTY = 15
# Triage's ``escalation`` for a clear escalation of a running story.
CLEAR_ESCALATION = 2
# Ceilings per story type; types not listed are uncapped.
STORY_TYPE_CAP = {"price_action": 40, "preview": 55, "opinion": 60, "press_release": 45}
# Kept in events (so they do not open duplicates) but never selected (spec §6.3 step 3).
UNSELECTABLE_STORY_TYPES = frozenset({"price_action"})


def market_score(
    *,
    story_type: str,
    impact: int,
    new_facts: bool,
    directness: int,
    escalation: int,
    variant: SelectionVariant,
) -> int:
    score = round(variant.impact_base[impact] * DIRECTNESS_FACTOR[directness])
    # A clear escalation of a running story (a new attack, a new supply loss)
    # is news even when the article repeats the background it builds on.
    waived = variant.escalation_waives_no_new_facts and escalation == CLEAR_ESCALATION
    if not new_facts and not waived:
        score -= NO_NEW_FACTS_PENALTY
    return max(0, min(score, STORY_TYPE_CAP.get(story_type, 100)))


def market_scores(result: TriageResult, *, variant: SelectionVariant) -> dict[str, int]:
    return {
        market: market_score(
            story_type=result.story_type,
            impact=result.impact,
            new_facts=result.new_facts,
            directness=directness,
            escalation=result.escalation,
            variant=variant,
        )
        for market, directness in result.directness.as_dict().items()
    }


def is_relevant(scores: dict[str, int]) -> bool:
    """Worth clustering and selecting: at least one market reaches the relevance bar."""
    return max(scores.values(), default=0) >= RELEVANT_SCORE


def triage_signals(result: TriageResult) -> dict[str, object]:
    """The raw judgements, stored so the formula can be recalibrated without new calls.

    Signals stored before triage answered ``escalation`` have none; read them as 0.
    ``event_date`` is an ISO date or None (only the event-date prompt answers it);
    signals stored without it read as None.
    """
    return {
        "impact": result.impact,
        "new_facts": result.new_facts,
        "directness": result.directness.as_dict(),
        "escalation": result.escalation,
        "event_date": None if result.event_date is None else result.event_date.isoformat(),
    }
