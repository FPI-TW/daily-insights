from datetime import UTC, datetime, timedelta

from daily_insights_api.modules.news.contracts import Candidate
from daily_insights_api.modules.news.extraction import FetchedCandidate
from daily_insights_api.modules.news.service import _limit_candidates


def _fetched(index: int, host: str, seen_at: datetime | None) -> FetchedCandidate:
    url = f"https://{host}/story-{index:03d}"
    return FetchedCandidate(
        Candidate(
            id=f"{index:064x}",
            url=url,
            hostname=host,
            source_name=host,
            headline=f"Story {index}",
            seen_at=seen_at,
        ),
        url,
        f"Body {index}",
        f"{index:064x}",
    )


def test_limit_prefers_newest_candidates_and_caps_each_source() -> None:
    base = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    # Alphabetically-first host publishes a burst of eight newest stories, the
    # other hosts are older; the cap must keep source diversity.
    burst = [_fetched(index, "apnews.com", base + timedelta(minutes=index)) for index in range(8)]
    others = [
        _fetched(100 + index, host, base - timedelta(hours=1 + index))
        for index, host in enumerate(("www.reuters.com", "www.bbc.com", "www.cnbc.com") * 3)
    ]

    limited = _limit_candidates(burst + others, total=12, per_source=5)

    assert len(limited) == 12
    hosts = [item.candidate.hostname for item in limited]
    assert hosts.count("apnews.com") == 5
    # The five apnews stories kept are the newest ones.
    assert [item.candidate.headline for item in limited[:5]] == [
        "Story 7",
        "Story 6",
        "Story 5",
        "Story 4",
        "Story 3",
    ]
    # Remaining slots go to the other sources in recency order.
    assert limited[5].candidate.hostname == "www.reuters.com"
    assert limited[6].candidate.hostname == "www.bbc.com"


def test_limit_is_deterministic_and_places_unknown_timestamps_last() -> None:
    seen = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    unknown = _fetched(1, "www.reuters.com", None)
    known_b = _fetched(2, "www.reuters.com", seen)
    known_a = _fetched(3, "apnews.com", seen)

    first = _limit_candidates([unknown, known_b, known_a], total=3, per_source=5)
    second = _limit_candidates([known_a, unknown, known_b], total=3, per_source=5)

    assert [item.source_url for item in first] == [item.source_url for item in second]
    assert first[-1] is unknown
    # Equal timestamps fall back to the URL for a stable order.
    assert [item.candidate.hostname for item in first[:2]] == ["apnews.com", "www.reuters.com"]


def test_limit_truncates_to_total() -> None:
    seen = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    candidates = [
        _fetched(index, f"host-{index % 6}.example", seen - timedelta(minutes=index))
        for index in range(40)
    ]
    assert len(_limit_candidates(candidates)) == 20


def test_cap_discovery_favours_full_text_candidates_within_the_total_budget() -> None:
    from daily_insights_api.modules.news.service import _cap_discovery

    base = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    candidates = [
        _fetched(index, f"host-{index % 4}.example", base + timedelta(minutes=index)).candidate
        for index in range(20)
    ]
    full_text = frozenset(candidate.id for candidate in candidates if candidate.id.endswith("3"))

    capped = _cap_discovery(candidates, per_source=3, total=6, full_text_ids=full_text)

    assert len(capped) == 6
    # Full-text candidates come first regardless of age; the rest are newest first.
    assert [candidate.id in full_text for candidate in capped][:2] == [True, True]
    per_host: dict[str, int] = {}
    for candidate in capped:
        per_host[candidate.hostname] = per_host.get(candidate.hostname, 0) + 1
    assert max(per_host.values()) <= 3
    assert len(_cap_discovery(candidates, per_source=10)) == 20


def test_interleaving_spreads_slots_across_sources_while_keeping_each_newest_first() -> None:
    from daily_insights_api.modules.news.service import _cap_discovery

    base = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    # One flash feed publishes twelve fresh items; two wires publish two each,
    # all older. Newest-first alone would hand every slot to the flash feed.
    flash = [
        _fetched(index, "flash.example", base + timedelta(minutes=index)) for index in range(12)
    ]
    wires = [
        _fetched(20 + index, "wire-a.example", base - timedelta(hours=1 + index))
        for index in range(2)
    ] + [
        _fetched(30 + index, "wire-b.example", base - timedelta(hours=2 + index))
        for index in range(2)
    ]
    candidates = [item.candidate for item in flash + wires]

    newest_first = _cap_discovery(candidates, per_source=5, total=6)
    assert {candidate.hostname for candidate in newest_first} == {"flash.example", "wire-a.example"}

    spread = _cap_discovery(candidates, per_source=5, total=6, interleave=True)
    assert [candidate.hostname for candidate in spread[:3]] == [
        "flash.example",
        "wire-a.example",
        "wire-b.example",
    ]
    assert sum(1 for candidate in spread if candidate.hostname == "flash.example") == 2
    flash_ids = [candidate.id for candidate in spread if candidate.hostname == "flash.example"]
    assert flash_ids == [flash[11].candidate.id, flash[10].candidate.id]

    limited = _limit_candidates(flash + wires, total=4, per_source=5, interleave=True)
    assert [item.candidate.hostname for item in limited] == [
        "flash.example",
        "wire-a.example",
        "wire-b.example",
        "flash.example",
    ]
