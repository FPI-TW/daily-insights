import hashlib
from datetime import UTC, datetime

from daily_insights_api.modules.news.collected_pool import merge_collected_candidates
from daily_insights_api.modules.news.contracts import Candidate


def _candidate(url: str, headline: str, seen_at: datetime | None = None) -> Candidate:
    return Candidate(
        id=hashlib.sha256(url.encode()).hexdigest(),
        url=url,
        hostname="news.example",
        source_name="News",
        headline=headline,
        seen_at=seen_at,
    )


def test_merge_without_collected_candidates_keeps_live_discovery_unchanged() -> None:
    live = [
        _candidate("https://news.example/a", "Fed holds rates steady"),
        _candidate("https://news.example/b", "Oil jumps on supply cut"),
    ]

    merged = merge_collected_candidates(live, [])

    assert merged.candidates == live
    assert merged.discovered_via == {candidate.id: "live" for candidate in live}
    assert (merged.live, merged.collected, merged.both, merged.duplicate_titles) == (2, 0, 0, 0)


def test_merge_dedupes_by_id_and_keeps_the_live_copy() -> None:
    live_only = _candidate("https://news.example/a", "Fed holds rates steady")
    shared_live = _candidate(
        "https://news.example/b", "Oil jumps on supply cut", datetime(2026, 9, 30, tzinfo=UTC)
    )
    shared_collected = _candidate(
        "https://news.example/b", "Oil jumps (earlier headline)", datetime(2026, 9, 29, tzinfo=UTC)
    )
    collected_only = _candidate("https://news.example/c", "Chipmakers rally after hours")

    merged = merge_collected_candidates(
        [live_only, shared_live], [collected_only, shared_collected]
    )

    assert merged.candidates == [live_only, shared_live, collected_only]
    assert merged.discovered_via == {
        live_only.id: "live",
        shared_live.id: "both",
        collected_only.id: "collected",
    }
    assert (merged.live, merged.collected, merged.both) == (1, 1, 1)


def test_merge_drops_collected_story_repeating_a_live_headline_under_another_url() -> None:
    live = _candidate("https://news.example/a", "Fed holds rates steady")
    repost = _candidate("https://news.example/a?ref=feed", "Fed holds rates steady")

    merged = merge_collected_candidates([live], [repost])

    assert merged.candidates == [live]
    assert merged.discovered_via == {live.id: "live"}
    assert merged.duplicate_titles == 1
