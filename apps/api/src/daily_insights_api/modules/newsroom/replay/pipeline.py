"""The pipeline functions a replay calls with a variant, in one place.

Scoring, ranking and quota code take the variant as a keyword-only argument;
keeping every such call here means a signature change touches this module
only. Everything else a replay runs (triage, the screens, the editor,
``write_draft``) reads the variant from ``Runtime.settings`` as in production.
"""

import uuid
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from typing import Any

from daily_insights_api.modules.newsroom import assembly, scoring
from daily_insights_api.modules.newsroom.contracts import TriageResult
from daily_insights_api.modules.newsroom.variants import SelectionVariant


def triage_result(
    *, topic: str | None, story_type: str | None, signals: dict[str, Any] | None
) -> TriageResult | None:
    """The stored judgements of one article as a ``TriageResult``; None when incomplete."""
    if not signals or topic is None or story_type is None:
        return None
    return TriageResult.model_validate(
        {
            "topic": topic,
            "story_type": story_type,
            "new_facts": signals.get("new_facts"),
            "impact": signals.get("impact"),
            "directness": signals.get("directness"),
            "escalation": signals.get("escalation", 0),
            # Stored from the event-date prompt on; absent reads as unknown.
            "event_date": signals.get("event_date"),
        }
    )


def market_scores(result: TriageResult, *, variant: SelectionVariant) -> dict[str, int]:
    return scoring.market_scores(result, variant=variant)


def is_relevant(scores: dict[str, int]) -> bool:
    return scoring.is_relevant(scores)


@contextmanager
def _unlimited_candidates() -> Iterator[None]:
    # The editor sees the top ``CANDIDATE_LIMIT``; tracking a story needs its
    # place further down too, under the very same ordering.
    limit = assembly.CANDIDATE_LIMIT
    assembly.CANDIDATE_LIMIT = 1_000_000
    try:
        yield
    finally:
        assembly.CANDIDATE_LIMIT = limit


def rank_candidates(
    titles: dict[uuid.UUID, str],
    articles: dict[uuid.UUID, list[Any]],
    market: str,
    *,
    excluded: Collection[uuid.UUID],
    variant: SelectionVariant,
    limit: bool = True,
) -> list[assembly.Candidate]:
    """``assembly.rank_candidates``; with ``limit=False`` every eligible event, same order."""
    if limit:
        return assembly.rank_candidates(
            titles, articles, market, excluded=excluded, variant=variant
        )
    with _unlimited_candidates():
        return assembly.rank_candidates(
            titles, articles, market, excluded=excluded, variant=variant
        )


def apply_quota(
    rated: list[assembly.Rated],
    story_groups: assembly.StoryGroups,
    *,
    variant: SelectionVariant,
) -> list[assembly.Rated]:
    # The quota rules are the same for every variant; the soft cap only flags.
    del variant
    return assembly.apply_quota(rated, story_groups)


def five_star_review(picks: list[assembly.Rated], *, variant: SelectionVariant) -> bool:
    """Whether the admin console would flag these picks for review (soft cap)."""
    return assembly.needs_five_star_review(
        sum(1 for pick in picks if pick.stars == 5), variant=variant
    )
