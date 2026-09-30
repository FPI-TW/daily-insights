"""Headline screening: rank the whole candidate pool before article extraction.

The model sees only headlines, source names and publish times, so it can read
the full overnight pool cheaply and hand a shortlist to extraction and the
existing selection flow. Everything here is deterministic bookkeeping around
the model call; the call itself runs through the refresh's durable checkpoint.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from daily_insights_api.modules.news.contracts import Candidate
from daily_insights_api.modules.news.editions import EditionSpec
from daily_insights_api.modules.news.extraction import FetchedCandidate
from daily_insights_api.modules.news.failures import NewsOperationError
from daily_insights_api.modules.news.llm import SCREEN_MAX_HEADLINES, HeadlineScreen
from daily_insights_api.modules.news.service import _market_impact_score

SCREEN_MAX_CALLS = 2
# Beyond two full calls the pool is cut by the pre-screen ranking, never silently.
SCREEN_MAX_POOL = SCREEN_MAX_HEADLINES * SCREEN_MAX_CALLS
# The shortlist is 1.5x the post-extraction retention (max_candidates * 2), so
# it absorbs extraction failures without fetching more than today's 80/100.
# Deriving it from EditionSpec keeps it in step if an edition's retention changes:
# global 20 * 2 * 1.5 = 60, market editions 30 * 2 * 1.5 = 90.
SHORTLIST_EXTRACTION_HEADROOM_NUMERATOR = 3
SHORTLIST_EXTRACTION_HEADROOM_DENOMINATOR = 2
# Validation failures that fall back to the regex ranking once repair is spent;
# every other model failure keeps the systemic news failure policy.
SCREEN_LOCAL_FAILURE_CODES = frozenset(
    {"screen_schema_invalid", "provider_invalid_json", "model_output_invalid"}
)


def shortlist_limit(spec: EditionSpec) -> int:
    return (
        spec.max_candidates
        * 2
        * SHORTLIST_EXTRACTION_HEADROOM_NUMERATOR
        // SHORTLIST_EXTRACTION_HEADROOM_DENOMINATOR
    )


def order_pool(
    candidates: Sequence[Candidate], impact_patterns: tuple[str, ...]
) -> list[Candidate]:
    """The pre-screen ranking: headline impact signals, then newest first."""
    return sorted(
        candidates,
        key=lambda candidate: (
            -_market_impact_score(candidate.headline, impact_patterns),
            candidate.seen_at is None,
            -(candidate.seen_at.timestamp() if candidate.seen_at else 0.0),
            str(candidate.url),
        ),
    )


def screen_batches(pool: Sequence[Candidate]) -> list[list[tuple[int, Candidate]]]:
    """Number the pool from 1 and split it into model calls of bounded size."""
    numbered = list(enumerate(pool, start=1))
    return [
        numbered[start : start + SCREEN_MAX_HEADLINES]
        for start in range(0, len(numbered), SCREEN_MAX_HEADLINES)
    ]


@dataclass(frozen=True)
class ScreenPick:
    candidate: Candidate
    rank: int
    score: int


def merge_screen_batches(
    answers: Sequence[tuple[Sequence[tuple[int, Candidate]], HeadlineScreen]], limit: int
) -> list[ScreenPick]:
    """Merge per-call answers by score, keeping each call's own order for ties."""
    ranked: list[tuple[int, int, int, Candidate]] = []
    for call_index, (headlines, answer) in enumerate(answers):
        by_number = dict(headlines)
        for position, item in enumerate(answer.shortlist):
            candidate = by_number.get(item.n)
            if candidate is not None:
                ranked.append((-item.score, call_index, position, candidate))
    ranked.sort(key=lambda entry: entry[:3])
    picks: list[ScreenPick] = []
    seen: set[str] = set()
    for negative_score, _, _, candidate in ranked:
        if candidate.id in seen:
            continue
        seen.add(candidate.id)
        picks.append(ScreenPick(candidate, rank=len(picks) + 1, score=-negative_score))
        if len(picks) >= limit:
            break
    return picks


ScreenStatus = Literal["shortlisted", "fallback", "empty"]


@dataclass(frozen=True)
class ScreenOutcome:
    status: ScreenStatus
    pool: int
    truncated: int = 0
    calls: int = 0
    # Candidate ids the model actually read; only these can be screened out.
    screened_ids: frozenset[str] = frozenset()
    picks: tuple[ScreenPick, ...] = ()
    fallback_code: str | None = None

    @property
    def applied(self) -> bool:
        return self.status == "shortlisted"

    @property
    def shortlist(self) -> list[Candidate]:
        return [pick.candidate for pick in self.picks]

    @property
    def ranks(self) -> dict[str, ScreenPick]:
        return {pick.candidate.id: pick for pick in self.picks}

    def summary(self) -> dict[str, object]:
        return {
            "status": self.status,
            "pool": self.pool,
            "truncated": self.truncated,
            "calls": self.calls,
            "screened": len(self.screened_ids),
            "shortlisted": len(self.picks),
            "fallback_code": self.fallback_code,
        }


def limit_screened_candidates(
    usable: Sequence[FetchedCandidate],
    ranks: Mapping[str, ScreenPick],
    *,
    total: int,
    per_source: int,
) -> list[FetchedCandidate]:
    """``_limit_candidates`` for a screened pool: screen rank replaces the regex order."""
    ordered = sorted(
        usable,
        key=lambda fetched: (
            ranks[fetched.candidate.id].rank if fetched.candidate.id in ranks else len(ranks) + 1,
            fetched.source_url,
        ),
    )
    per_host: dict[str, int] = {}
    limited: list[FetchedCandidate] = []
    for fetched in ordered:
        host = fetched.candidate.hostname
        if per_host.get(host, 0) >= per_source:
            continue
        per_host[host] = per_host.get(host, 0) + 1
        limited.append(fetched)
        if len(limited) == total:
            break
    return limited


def screen_input_digest(base_digest: str, prompt_digest: str, prompt_version: str) -> str:
    """Fold the screen prompt into an edition digest; used only while screening is on."""
    return hashlib.sha256(
        json.dumps(
            {
                "base": base_digest,
                "screen_prompt_digest": prompt_digest,
                "screen_prompt_version": prompt_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def screen_failure_is_local(error: NewsOperationError) -> bool:
    """Whether an exhausted screen repair may fall back instead of failing the run."""
    failure = error.failure
    return (
        failure.action == "attention"
        and failure.code.endswith("_exhausted")
        and failure.code.removesuffix("_exhausted") in SCREEN_LOCAL_FAILURE_CODES
    )
