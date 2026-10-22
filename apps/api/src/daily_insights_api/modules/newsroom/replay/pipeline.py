"""The pipeline functions a replay calls with a variant, in one place.

Scoring and quota code takes the variant as a keyword-only argument once the
P0 work lands; keeping every such call here means a signature change touches
this module only. Everything else a replay runs (triage, the screens, the
editor, ``write_draft``) goes through ``Runtime.settings`` as in production.
"""

import uuid
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from typing import Any

from daily_insights_api.modules.newsroom import assembly, scoring
from daily_insights_api.modules.newsroom.contracts import TriageResult
from daily_insights_api.modules.newsroom.variants import BASELINE, SelectionVariant


class VariantUnsupportedError(RuntimeError):
    """This branch's scoring code cannot build ``variant`` yet."""


def require_supported(variant: SelectionVariant) -> None:
    if variant is not BASELINE:
        raise VariantUnsupportedError(
            f"variant {variant.name!r} needs the P0 scoring code; "
            "this branch only reproduces the baseline"
        )


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
        }
    )


def market_scores(result: TriageResult, *, variant: SelectionVariant) -> dict[str, int]:
    require_supported(variant)
    return scoring.market_scores(result)


def is_relevant(scores: dict[str, int], *, variant: SelectionVariant) -> bool:
    require_supported(variant)
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
    require_supported(variant)
    if limit:
        return assembly.rank_candidates(titles, articles, market, excluded=excluded)
    with _unlimited_candidates():
        return assembly.rank_candidates(titles, articles, market, excluded=excluded)


def apply_quota(
    rated: list[assembly.Rated],
    story_groups: assembly.StoryGroups,
    *,
    variant: SelectionVariant,
) -> list[assembly.Rated]:
    require_supported(variant)
    return assembly.apply_quota(rated, story_groups)


def five_star_review(picks: list[assembly.Rated], *, variant: SelectionVariant) -> bool:
    """Whether the variant's soft cap would flag these picks for review."""
    cap = variant.five_star_soft_cap
    return cap is not None and sum(1 for pick in picks if pick.stars == 5) > cap
