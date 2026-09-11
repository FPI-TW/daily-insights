from datetime import UTC, datetime, timedelta

from daily_insights_api.modules.news.contracts import Candidate
from daily_insights_api.modules.news.editions import GLOBAL_SPEC, TW_EQUITY_SPEC, US_EQUITY_SPEC
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


def test_global_candidate_limits_keep_systemic_catalysts_ahead_of_newer_company_news() -> None:
    from daily_insights_api.modules.news.service import _cap_discovery

    base = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    headlines = [
        "CNBC Daily Open: Apple foldable debuts as bond vigilantes retreat",
        "Retailer unveils a new customer loyalty programme",
        "Technology company adds a cybersecurity director",
        "Media group reports a strong summer box office",
        "Payments companies announce an AI partnership",
        "Treasury Department to buy back longer-term debt at triple the normal level",
        "U.S. import ban on Canadian goods escalates trade war",
        "ECB rate decision and U.S. PPI set the global market tone",
    ]
    candidates = [
        _fetched(
            index,
            "www.cnbc.com",
            base - timedelta(minutes=index),
        ).candidate.model_copy(update={"headline": headline})
        for index, headline in enumerate(headlines)
    ]

    capped = _cap_discovery(
        candidates,
        per_source=5,
        total=3,
        impact_patterns=GLOBAL_SPEC.headline_impact_patterns,
    )

    selected_headlines = {candidate.headline for candidate in capped}
    assert selected_headlines == set(headlines[-3:])

    fetched = [
        FetchedCandidate(
            candidate,
            str(candidate.url),
            f"Body for {candidate.headline}",
            candidate.id,
        )
        for candidate in candidates
    ]
    limited = _limit_candidates(
        fetched,
        per_source=5,
        total=3,
        impact_patterns=GLOBAL_SPEC.headline_impact_patterns,
    )
    limited_headlines = {item.candidate.headline for item in limited}
    assert selected_headlines == limited_headlines


def test_taiwan_candidate_limits_use_taiwan_market_signals() -> None:
    from daily_insights_api.modules.news.service import _cap_discovery

    base = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    headlines = [
        "Celebrity opens a restaurant in Taipei",
        "New smartphone colour launches in Taiwan",
        "Taipei luxury-home listing reaches a record price",
        "台股加權指數重挫 外資賣超擴大",
        "台積電上調先進製程資本支出與營收展望",
        "金管會公布影響上市公司的新規則",
    ]
    candidates = [
        _fetched(index, "news.example", base - timedelta(minutes=index)).candidate.model_copy(
            update={"headline": headline}
        )
        for index, headline in enumerate(headlines)
    ]

    capped = _cap_discovery(
        candidates,
        per_source=3,
        total=3,
        impact_patterns=TW_EQUITY_SPEC.headline_impact_patterns,
    )

    assert {candidate.headline for candidate in capped} == set(headlines[3:])


def test_us_candidate_limits_use_us_market_signals() -> None:
    from daily_insights_api.modules.news.service import _cap_discovery

    base = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    headlines = [
        "Hollywood studio releases a movie trailer",
        "Apple reveals another iPhone accessory",
        "Retail chain opens a store in California",
        "Fed inflation surprise sends the S&P 500 lower",
        "Treasury yields jump after a Fed rate decision",
        "Nvidia earnings guidance lifts the semiconductor sector",
    ]
    candidates = [
        _fetched(index, "news.example", base - timedelta(minutes=index)).candidate.model_copy(
            update={"headline": headline}
        )
        for index, headline in enumerate(headlines)
    ]

    capped = _cap_discovery(
        candidates,
        per_source=3,
        total=3,
        impact_patterns=US_EQUITY_SPEC.headline_impact_patterns,
    )

    assert {candidate.headline for candidate in capped} == set(headlines[3:])


def test_us_candidate_limits_keep_material_chip_catalyst_ahead_of_routine_financing() -> None:
    from daily_insights_api.modules.news.service import _cap_discovery

    base = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    headlines = [
        "Simon Property issues $800m senior notes",
        "Dollar General discusses its AI business strategy",
        "UBS sends investors a message about the economy",
        "英特爾兩日狂飆逾10% CPU喊漲10% 輝達入股帶動AI想像",
    ]
    candidates = [
        _fetched(index, "news.example", base - timedelta(minutes=index)).candidate.model_copy(
            update={"headline": headline}
        )
        for index, headline in enumerate(headlines)
    ]

    capped = _cap_discovery(
        candidates,
        per_source=1,
        total=1,
        impact_patterns=US_EQUITY_SPEC.headline_impact_patterns,
    )

    assert [candidate.headline for candidate in capped] == [headlines[-1]]


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


def test_candidate_ledger_records_the_furthest_stage_each_candidate_reached() -> None:
    import uuid

    from daily_insights_api.modules.news.contracts import SelectedCandidate, Selection
    from daily_insights_api.modules.news.llm import ModelCall
    from daily_insights_api.modules.news.service import _CandidateLedger

    fetched = [_fetched(index, f"host{index}.example", None) for index in range(1, 8)]
    discovered = [item.candidate for item in fetched]
    ledger = _CandidateLedger(discovered)
    # Candidate 7 is cut before extraction, 6 fails extraction, 5 is extracted
    # but never reaches a prompt.
    ledger.fetching(discovered[:6])
    ledger.extracted(fetched[:5])
    ledger.reviewed(fetched[:4])

    def pick(index: int, market: str = "global") -> SelectedCandidate:
        return SelectedCandidate(
            id=fetched[index - 1].candidate.id,
            topic="markets",
            event_key=f"event-{index}",
            market=market,
            importance=3,
        )

    returned = (pick(1), pick(2, "asia"), pick(3))
    ledger.returned(
        ModelCall(
            Selection(selections=(pick(1), pick(3))),
            None,
            None,
            None,
            1,
            "a" * 64,
            rejected=((pick(2, "asia"), "off_market"),),
            returned=returned,
        )
    )
    ledger.drop(fetched[2].candidate.id, "summary_failed")
    item_id = uuid.uuid4()
    ledger.published(fetched[0].candidate.id, item_id)
    edition_id = uuid.uuid4()
    rows = {row.candidate_id: row for row in ledger.rows(edition_id)}
    assert all(row.edition_id == edition_id for row in rows.values())
    by_index = {index: rows[fetched[index - 1].candidate.id] for index in range(1, 8)}
    assert [(by_index[i].stage, by_index[i].drop_reason) for i in range(1, 8)] == [
        ("published", None),
        ("dropped", "off_market"),
        ("dropped", "summary_failed"),
        ("reviewed", None),
        ("unused", None),
        ("fetch_failed", None),
        ("discovered", None),
    ]
    assert by_index[1].item_id == item_id
    # The model's original order survives filtering: "asia" was second.
    assert [by_index[i].ai_rank for i in (1, 2, 3, 4)] == [1, 2, 3, None]
    assert by_index[2].ai_market == "asia" and by_index[5].content_digest is not None
    assert by_index[6].content_digest is None and by_index[7].seen_at is None
