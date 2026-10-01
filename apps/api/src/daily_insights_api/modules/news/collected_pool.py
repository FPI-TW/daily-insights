"""Merge the overnight collection pool into the 08:00 live discovery.

The collector only stores feed metadata, so a collected candidate carries no
body; extraction fetches its article page like any other undelivered story.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.news.contracts import Candidate
from daily_insights_api.modules.news.extraction import _dedupe_candidates, allowed_hostname
from daily_insights_api.modules.news.models import NewsCollectedCandidate

# Same eligibility as live discovery: the 24 hours before the refresh runs.
COLLECTED_WINDOW = timedelta(hours=24)

DiscoveredVia = Literal["live", "collected", "both"]


@dataclass(frozen=True)
class CollectedPool:
    candidates: list[Candidate]
    invalid: int = 0


@dataclass(frozen=True)
class MergedDiscovery:
    candidates: list[Candidate]
    discovered_via: dict[str, DiscoveredVia]
    live: int
    collected: int
    both: int
    duplicate_titles: int


async def load_collected_candidates(
    database: AsyncSession,
    *,
    market: str,
    allowed: frozenset[str],
    now: datetime,
) -> CollectedPool:
    """Read pool rows tagged for ``market`` whose effective time is in the window.

    The effective time is ``seen_at``, or ``first_collected_at`` for undated
    feed items, so an undated story cannot stay eligible indefinitely. Rows are
    ordered deterministically so retries of one refresh see the same list.
    """
    effective_at = func.coalesce(
        NewsCollectedCandidate.seen_at, NewsCollectedCandidate.first_collected_at
    )
    rows = await database.scalars(
        select(NewsCollectedCandidate)
        .where(
            NewsCollectedCandidate.markets.contains([market]),
            effective_at >= now - COLLECTED_WINDOW,
            effective_at <= now,
        )
        .order_by(effective_at.desc(), NewsCollectedCandidate.candidate_id)
    )
    candidates: list[Candidate] = []
    invalid = 0
    for row in rows:
        if not allowed_hostname(row.hostname, allowed):
            continue
        try:
            candidates.append(
                Candidate(
                    id=row.candidate_id,
                    url=row.url,
                    hostname=row.hostname,
                    source_name=row.source_name,
                    headline=row.headline[:1000],
                    seen_at=row.seen_at,
                )
            )
        except ValidationError:
            invalid += 1
    return CollectedPool(candidates, invalid)


def merge_collected_candidates(
    live: list[Candidate], collected: list[Candidate]
) -> MergedDiscovery:
    """Union live and collected candidates by id, keeping the live copy.

    Live candidates keep their order and always survive; collected-only
    candidates follow and pass the same headline dedupe live discovery uses,
    so a story re-published under another URL does not enter twice.
    """
    live_ids = {candidate.id for candidate in live}
    collected_ids = {candidate.id for candidate in collected}
    collected_only = [candidate for candidate in collected if candidate.id not in live_ids]
    merged = _dedupe_candidates([*live, *collected_only])
    discovered_via: dict[str, DiscoveredVia] = {
        candidate.id: (
            "both"
            if candidate.id in live_ids and candidate.id in collected_ids
            else "live"
            if candidate.id in live_ids
            else "collected"
        )
        for candidate in merged
    }
    counts = {
        via: sum(1 for value in discovered_via.values() if value == via)
        for via in ("live", "collected", "both")
    }
    return MergedDiscovery(
        candidates=merged,
        discovered_via=discovered_via,
        live=counts["live"],
        collected=counts["collected"],
        both=counts["both"],
        duplicate_titles=len(live) + len(collected_only) - len(merged),
    )
