import json
import uuid
from datetime import UTC, date, datetime

import pytest

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.analysis import (
    filter_related_symbols,
    resolve_symbol,
    symbol_catalog,
)
from daily_insights_api.modules.newsroom.assembly import (
    EDITOR_SUMMARY_CHARS,
    STORY_GROUP_LIMIT,
    ArticleSignal,
    Candidate,
    EditorContractError,
    PublishedEvent,
    Rated,
    _ArticleRow,
    _editor_payload,
    _signal,
    apply_quota,
    event_brief,
    event_score,
    fallback_selection,
    key_figures,
    lead_summary,
    same_story,
    same_story_titles,
    validate_duplicate_groups,
    validate_ratings,
)
from daily_insights_api.modules.newsroom.contracts import (
    DuplicateGroups,
    EditorRating,
    EditorResult,
)
from daily_insights_api.modules.newsroom.models import NewsroomEditionItem, NewsroomEvent
from daily_insights_api.modules.newsroom.translation import (
    _english_is_current,
    load_prompt,
    to_zh_hans,
    zh_hant_digest,
)
from daily_insights_api.modules.newsroom.variants import BASELINE, P0
from daily_insights_api.modules.orchestration.functions import _run_newsroom_assemble
from daily_insights_api.modules.orchestration.worker import ClaimedFunction


def _rated(stars: int, score: float, name: str | None = None) -> Rated:
    return Rated(uuid.uuid5(uuid.NAMESPACE_URL, name or f"{stars}-{score}"), stars, score)


def _stars(selection: list[Rated]) -> list[int]:
    return [item.stars for item in selection]


# --- D8 quota ---------------------------------------------------------------------


def test_quota_keeps_every_five_star_event() -> None:
    rated = [_rated(5, float(score)) for score in range(9)]
    assert _stars(apply_quota(rated)) == [5] * 9


def test_quota_caps_four_star_events_at_five_by_score() -> None:
    rated = [_rated(4, float(score)) for score in range(8)]
    selection = apply_quota(rated)
    assert _stars(selection) == [4] * 5
    assert [item.score for item in selection] == [7.0, 6.0, 5.0, 4.0, 3.0]


def test_quota_five_and_four_stars_combine_without_fillers() -> None:
    rated = [
        *(_rated(5, float(score)) for score in range(2)),
        *(_rated(4, float(score)) for score in range(7)),
        *(_rated(3, 99.0 + score) for score in range(3)),
    ]
    assert _stars(apply_quota(rated)) == [5, 5, 4, 4, 4, 4, 4]


def test_quota_fills_with_three_stars_only_below_five() -> None:
    rated = [
        _rated(5, 10.0),
        _rated(4, 20.0),
        _rated(3, 5.0),
        _rated(2, 80.0),
        _rated(3, 50.0),
        _rated(1, 90.0),
    ]
    selection = apply_quota(rated)
    # Two high-star events leave three slots; only the two 3-star events fill them.
    assert [(item.stars, item.score) for item in selection] == [
        (5, 10.0),
        (4, 20.0),
        (3, 50.0),
        (3, 5.0),
    ]


def test_quota_exactly_five_high_stars_takes_no_fillers() -> None:
    rated = [*(_rated(4, float(score)) for score in range(5)), _rated(3, 100.0)]
    assert _stars(apply_quota(rated)) == [4] * 5


def test_quota_fills_at_most_five_when_nothing_scores_high() -> None:
    rated = [_rated(stars, float(score)) for stars in (1, 2, 3) for score in range(7)]
    selection = apply_quota(rated)
    assert _stars(selection) == [3] * 5
    assert [item.score for item in selection] == [6.0, 5.0, 4.0, 3.0, 2.0]


def test_quota_never_fills_with_one_or_two_stars() -> None:
    assert _stars(apply_quota([_rated(4, 1.0), _rated(2, 9.0), _rated(1, 2.0)])) == [4]
    assert apply_quota([_rated(2, 9.0), _rated(1, 2.0)]) == []


def test_quota_handles_empty_input() -> None:
    assert apply_quota([]) == []


def test_quota_four_star_cap_does_not_count_five_stars() -> None:
    rated = [
        *(_rated(5, float(score)) for score in range(6)),
        *(_rated(4, float(score)) for score in range(6)),
    ]
    assert _stars(apply_quota(rated)) == [5] * 6 + [4] * 5


def test_quota_orders_by_stars_then_score_descending() -> None:
    rated = [_rated(4, 90.0), _rated(5, 1.0), _rated(4, 95.0), _rated(5, 3.0)]
    assert [(item.stars, item.score) for item in apply_quota(rated)] == [
        (5, 3.0),
        (5, 1.0),
        (4, 95.0),
        (4, 90.0),
    ]


def test_quota_breaks_exact_ties_deterministically() -> None:
    rated = [_rated(3, 1.0, name) for name in ("a", "b", "c", "d", "e", "f")]
    assert apply_quota(rated) == apply_quota(list(reversed(rated)))
    assert len(apply_quota(rated)) == 5


def test_quota_breaks_ties_by_candidate_position() -> None:
    first, second = uuid.UUID(int=2), uuid.UUID(int=1)
    rated = [Rated(second, 4, 70.0, position=1), Rated(first, 4, 70.0, position=0)]
    assert [item.event_id for item in apply_quota(rated)] == [first, second]


def test_quota_keeps_two_events_per_story_group() -> None:
    # One earnings call split into five events, all rated high (2026-10-16 tw_equity).
    call = [_rated(5, 135.0), _rated(5, 118.0), _rated(4, 106.0), _rated(4, 97.0), _rated(4, 92.0)]
    other = [_rated(4, 80.0), _rated(3, 100.0), _rated(3, 90.0)]
    groups = {item.event_id: 0 for item in call}
    selection = apply_quota(call + other, groups)
    assert [(item.stars, item.score) for item in selection] == [
        (5, 135.0),
        (5, 118.0),
        (4, 80.0),
        (3, 100.0),
        (3, 90.0),
    ]
    assert sum(item.event_id in groups for item in selection) == STORY_GROUP_LIMIT


def test_quota_story_limit_skips_group_members_before_counting_four_stars() -> None:
    grouped = [_rated(4, 99.0 - index) for index in range(4)]
    loose = [_rated(4, 50.0 - index) for index in range(4)]
    groups = {item.event_id: 7 for item in grouped}
    selection = apply_quota(grouped + loose, groups)
    assert [item.score for item in selection] == [99.0, 98.0, 50.0, 49.0, 48.0]


def test_fallback_keeps_two_events_per_story_group() -> None:
    scores = [(uuid.UUID(int=index), float(100 - index)) for index in range(8)]
    groups = {scores[0][0]: 0, scores[1][0]: 0, scores[2][0]: 0, scores[3][0]: 1}
    assert [score for _, score in fallback_selection(scores, groups)] == [
        100.0,
        99.0,
        97.0,
        96.0,
        95.0,
    ]


# --- Scoring ------------------------------------------------------------------------


def test_event_score_uses_best_weighted_article_plus_source_bonus() -> None:
    source_a, source_b, source_c = "Reuters", "CNA", "UDN"
    signals = [
        ArticleSignal(source_a, 60, 1.5),
        ArticleSignal(source_a, 80, 1.0),
        ArticleSignal(source_b, 70, 0.5),
        ArticleSignal(source_c, 10, 2.0),
    ]
    assert event_score(signals) == 90.0 + 10.0


def test_event_score_caps_multi_source_bonus_at_twenty() -> None:
    signals = [ArticleSignal(f"outlet-{index}", 50, 1.0) for index in range(9)]
    assert event_score(signals) == 70.0


def test_event_score_single_source_has_no_bonus() -> None:
    source = "Reuters"
    assert event_score([ArticleSignal(source, 40, 1.0), ArticleSignal(source, 30, 1.0)]) == 40.0
    assert event_score([]) == 0.0


def test_story_type_cap_holds_after_the_source_weight() -> None:
    # An opinion piece at its 60 ceiling stays at 60 from a 1.2-weighted source.
    assert event_score([ArticleSignal("CNA", 60, 1.2, cap=60)]) == 60.0
    assert event_score([ArticleSignal("CNA", 40, 1.2, cap=60)]) == 48.0
    assert event_score([ArticleSignal("CNA", 82, 1.2)]) == pytest.approx(98.4)


def test_fallback_takes_top_five_by_score() -> None:
    scores = [(uuid.uuid4(), float(score)) for score in range(8)]
    assert [score for _, score in fallback_selection(scores)] == [7.0, 6.0, 5.0, 4.0, 3.0]


def test_fallback_keeps_the_given_order_on_equal_scores() -> None:
    scores = [(uuid.UUID(int=index), 50.0) for index in (9, 3, 7)]
    assert fallback_selection(scores) == scores


# --- Editor contract ------------------------------------------------------------------


def _candidate(name: str, headlines: tuple[tuple[str, str], ...] | None = None) -> Candidate:
    return Candidate(
        event_id=uuid.uuid5(uuid.NAMESPACE_URL, name),
        working_title=name,
        score=50.0,
        headlines=headlines or ((name, "CNA"),),
        article_count=1,
        source_count=1,
        trust_tier=1,
        first_seen_at=datetime(2026, 10, 16, tzinfo=UTC),
    )


def _row(
    event: Candidate,
    title: str,
    *,
    source: str = "CNA",
    story_type: str | None = "catalyst",
    impact: int | None = 3,
    escalation: int = 0,
    summary: str | None = None,
    minute: int = 0,
) -> _ArticleRow:
    return _ArticleRow(
        event_id=event.event_id,
        title=title,
        source_id=uuid.uuid5(uuid.NAMESPACE_URL, source),
        source_name=source,
        weight=1.0,
        trust_tier=2,
        market_scores={},
        has_body=True,
        story_type=story_type,
        first_seen_at=datetime(2026, 10, 16, 0, minute, tzinfo=UTC),
        feed_summary=summary,
        impact=impact,
        escalation=escalation,
    )


def test_editor_payload_labels_story_groups_present_in_the_market() -> None:
    a, b, c, d, e = (_candidate(name) for name in "abcde")
    # Group 3 has only one candidate in this market, so it carries no label.
    groups = {c.event_id: 5, a.event_id: 5, b.event_id: 3, e.event_id: 9, d.event_id: 9}
    payload = _editor_payload("tw_equity", [a, b, c, d], {}, groups, variant=BASELINE, articles={})
    assert [event.get("story_group") for event in payload["events"]] == ["g1", None, "g1", None]
    reordered = _editor_payload(
        "tw_equity", [d, e, a, c], {}, groups, variant=BASELINE, articles={}
    )
    assert reordered["events"][0]["story_group"] == "g1"


def _payload_fixture() -> tuple[
    list[Candidate],
    dict[uuid.UUID, PublishedEvent],
    dict[uuid.UUID, int],
    dict[uuid.UUID, list[_ArticleRow]],
]:
    first = _candidate("荷姆茲", (("油輪遇襲", "Reuters"), ("航道受阻", "CNA")))
    second = _candidate("聯準會", (("聯準會按兵不動", "CNBC"),))
    third = _candidate("台積電", (("台積電法說", "經濟日報"),))
    follow_ups = {
        first.event_id: PublishedEvent(
            event_id=uuid.uuid5(uuid.NAMESPACE_URL, "published"),
            edition_date=date(2026, 10, 20),
            working_title="荷姆茲",
            headline="油輪遇襲",
            summary="",
        )
    }
    groups = {second.event_id: 1, third.event_id: 1}
    articles = {
        first.event_id: [
            _row(first, "油輪遇襲", source="Reuters", impact=5, escalation=2),
            _row(first, "航道受阻", summary="船隻改道。保費上升。油價走高。"),
        ],
        second.event_id: [_row(second, "聯準會按兵不動", source="CNBC", story_type="official")],
    }
    return [first, second, third], follow_ups, groups, articles


def _wire(payload: dict[str, object]) -> str:
    # How providers.JsonModel serialises the payload into the request.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def test_baseline_editor_payload_is_unchanged_byte_for_byte() -> None:
    candidates, follow_ups, groups, articles = _payload_fixture()
    payload = _editor_payload(
        "global", candidates, follow_ups, groups, variant=BASELINE, articles=articles
    )
    first, second, third = (str(candidate.event_id) for candidate in candidates)
    assert _wire(payload) == (
        '{"market":"global","events":['
        f'{{"event_id":"{first}","working_title":"荷姆茲","report_count":1,"source_count":1,'
        '"headlines":[{"title":"油輪遇襲","source":"Reuters"},{"title":"航道受阻","source":"CNA"}],'
        '"earlier_coverage":{"edition_date":"2026-10-20","headline":"油輪遇襲"}},'
        f'{{"event_id":"{second}","working_title":"聯準會","report_count":1,"source_count":1,'
        '"headlines":[{"title":"聯準會按兵不動","source":"CNBC"}],"story_group":"g1"},'
        f'{{"event_id":"{third}","working_title":"台積電","report_count":1,"source_count":1,'
        '"headlines":[{"title":"台積電法說","source":"經濟日報"}],"story_group":"g1"}]}'
    )


def test_p0_editor_payload_adds_impact_type_summary_and_escalation() -> None:
    candidates, follow_ups, groups, articles = _payload_fixture()
    payload = _editor_payload(
        "global", candidates, follow_ups, groups, variant=P0, articles=articles
    )
    first, second, third = payload["events"]
    assert list(first) == [
        "event_id",
        "working_title",
        "report_count",
        "source_count",
        "max_impact",
        "max_escalation",
        "main_story_type",
        "headlines",
        "lead_summary",
        "earlier_coverage",
    ]
    # The lead headline has no summary, so the next one shown lends its first two sentences.
    assert (first["max_impact"], first["max_escalation"], first["main_story_type"]) == (
        5,
        2,
        "catalyst",
    )
    assert first["lead_summary"] == "船隻改道。保費上升。"
    assert (second["max_impact"], second["main_story_type"], second["story_group"]) == (
        3,
        "official",
        "g1",
    )
    assert "lead_summary" not in second
    # An event without loaded reports still carries every key the prompt describes.
    assert (third["max_impact"], third["max_escalation"], third["main_story_type"]) == (
        None,
        0,
        None,
    )


def test_main_story_type_skips_price_action_and_breaks_ties_on_impact() -> None:
    event = _candidate("e")
    rows = [
        _row(event, "盤勢1", story_type="price_action", impact=5, escalation=2),
        _row(event, "盤勢2", story_type="price_action"),
        _row(event, "盤勢3", story_type="price_action"),
        _row(event, "評論", story_type="opinion", impact=2),
        _row(event, "數據", story_type="data", impact=4),
    ]
    brief = event_brief(rows, event.headlines)
    # Price-action pieces neither name the type nor lend their impact or escalation.
    assert (brief.main_story_type, brief.max_impact, brief.max_escalation) == ("data", 4, 0)
    tied = [
        _row(event, "a", story_type="opinion", impact=3),
        _row(event, "b", story_type="catalyst", impact=3),
    ]
    assert event_brief(tied, event.headlines).main_story_type == "opinion"
    assert event_brief([_row(event, "x", story_type="price_action")], ()).main_story_type is None


def test_lead_summary_follows_the_headline_order() -> None:
    event = _candidate("e", (("B", "Reuters"), ("A", "CNA")))
    rows = [
        _row(event, "A", summary="甲摘要。"),
        _row(event, "B", source="Reuters", summary="乙摘要。"),
        _row(event, "C", summary="丙摘要。"),
    ]
    assert event_brief(rows, event.headlines).lead_summary == "乙摘要。"
    # A summary that repeats its headline, or a blank one, is passed over.
    rows[1] = _row(event, "B", source="Reuters", summary=" B ")
    assert event_brief(rows, event.headlines).lead_summary == "甲摘要。"


def test_lead_summary_keeps_two_sentences_within_the_limit() -> None:
    assert lead_summary(None) is None
    assert lead_summary("  ") is None
    assert lead_summary("Fed holds.\n Markets  rise.") == "Fed holds. Markets rise."
    assert lead_summary("一。二。三。") == "一。二。"
    assert lead_summary("The U.S. economy grew. Jobs rose. Prices fell.") == (
        "The U.S. economy grew. Jobs rose."
    )
    assert lead_summary("A&#8230; B&#10;C") == "A… B C"
    long_sentence = "甲" * 100 + "。" + "乙" * 100 + "。"
    assert lead_summary(long_sentence) == "甲" * 100 + "。"
    unbroken = "x" * 400
    cut = lead_summary(unbroken)
    assert cut == "x" * EDITOR_SUMMARY_CHARS + "…"
    # A sentence end too early to keep falls back to the hard cut.
    assert lead_summary("短。" + "長" * 300) == ("短。" + "長" * 300)[:EDITOR_SUMMARY_CHARS] + "…"


@pytest.mark.parametrize(
    ("signals", "impact", "escalation"),
    [
        ({"impact": 4, "escalation": 2}, 4, 2),
        # Signals stored before triage v3 have no escalation.
        ({"impact": 5, "new_facts": True, "stale": False}, 5, None),
        ({"impact": 9, "escalation": 3}, None, None),
        ({"impact": True, "escalation": "2"}, None, None),
        (None, None, None),
    ],
)
def test_stored_triage_signals_are_read_defensively(
    signals: object, impact: int | None, escalation: int | None
) -> None:
    assert _signal(signals, "impact", 1, 5) == impact
    assert _signal(signals, "escalation", 0, 2) == escalation


def _result(*pairs: tuple[str, int]) -> EditorResult:
    return EditorResult(ratings=[EditorRating(event_id=key, stars=stars) for key, stars in pairs])


def test_editor_ratings_must_cover_every_event_once() -> None:
    assert validate_ratings(_result(("a", 5), ("b", 3)), {"a", "b"}) == {"a": 5, "b": 3}
    with pytest.raises(EditorContractError):
        validate_ratings(_result(("a", 5)), {"a", "b"})
    with pytest.raises(EditorContractError):
        validate_ratings(_result(("a", 5), ("b", 3), ("c", 1)), {"a", "b"})
    with pytest.raises(EditorContractError):
        validate_ratings(_result(("a", 5), ("a", 4), ("b", 3)), {"a", "b"})


# --- Symbols, zh-hans, digest, prompts --------------------------------------------------


def test_symbol_catalog_follows_site_dashboards() -> None:
    catalog = symbol_catalog()
    assert catalog["^TWII"].kind == "index"
    assert catalog["XAU/USD"].kind == "commodity"
    assert catalog["USD/TWD"].kind == "fx"
    assert catalog["BTC/USD"].kind == "crypto"
    assert catalog["NVDA"].kind == "equity"
    assert catalog["NVDA"].market_code == "us_equity"
    assert resolve_symbol("twii") == catalog["^TWII"]
    assert resolve_symbol("BTC") == catalog["BTC/USD"]
    assert resolve_symbol("2330.TW") is None


def test_related_symbols_drop_anything_without_a_dashboard() -> None:
    kept = filter_related_symbols(
        [
            {"symbol": "nvda", "kind": "index", "label": "輝達"},
            {"symbol": "2330.TW", "kind": "equity", "label": "台積電"},
            {"symbol": "NVDA", "kind": "equity", "label": "重複"},
            {"symbol": "^TWII", "kind": "index", "label": " "},
            {"symbol": "^SOX", "kind": "index", "label": "費半"},
        ]
    )
    assert kept == [
        {"symbol": "NVDA", "kind": "equity", "label": "輝達"},
        {"symbol": "^SOX", "kind": "index", "label": "費半"},
    ]


def test_zh_hans_uses_opencc_taiwan_phrases() -> None:
    assert to_zh_hans("聯準會升息 滑鼠與軟體類股走強") == "联准会升息 鼠标与软件类股走强"


def _item(**values: object) -> NewsroomEditionItem:
    return NewsroomEditionItem(id=uuid.uuid4(), **values)


def test_english_needs_a_why_only_for_items_whose_why_is_finished() -> None:
    event = NewsroomEvent(headline_en="Headline", summary_en="Summary", en_source_digest="d")
    # Legacy items have no "why"; a "why" still being written has nothing to translate.
    legacy = _item(why_zh_hant=None, why_status="ready", why_en=None, why_en_status="idle")
    writing = _item(why_zh_hant=None, why_status="pending", why_en=None, why_en_status="idle")
    assert _english_is_current(event, [legacy, writing], "d")
    translated = _item(why_zh_hant="為何", why_status="ready", why_en="Why", why_en_status="ready")
    assert _english_is_current(event, [legacy, translated], "d")
    # A rewritten "why" resets its English status, which asks for a new translation.
    rewritten = _item(why_zh_hant="改寫", why_status="ready", why_en="Why", why_en_status="idle")
    assert not _english_is_current(event, [rewritten], "d")
    assert not _english_is_current(event, [translated], "other")


def test_zh_hant_digest_covers_the_headline_and_summary() -> None:
    digest = zh_hant_digest("標題", "摘要")
    assert digest == zh_hant_digest("標題", "摘要")
    assert len(digest) == 64
    assert digest != zh_hant_digest("標題改", "摘要")
    assert digest != zh_hant_digest("標題", "摘要改")


@pytest.mark.parametrize("name", ["editor", "dedupe", "repeat", "analysis", "why", "translate"])
def test_prompts_ship_with_the_package(name: str) -> None:
    assert len(load_prompt(name)) > 100


# --- Orchestration handler -------------------------------------------------------------


def _claimed(edition_date: date) -> ClaimedFunction:
    return ClaimedFunction(
        connection=None,  # type: ignore[arg-type]
        function_run_id=uuid.uuid4(),
        job_run_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        function_key="newsroom_assemble",
        provider_key="newsroom",
        edition_date=edition_date,
        fence_token=uuid.uuid4(),
        deadline_at=None,
        scope={},
    )


async def test_newsroom_assemble_is_a_successful_no_op_while_disabled() -> None:
    settings = Settings(newsroom_enabled=False)
    outcome = await _run_newsroom_assemble(
        settings,
        None,  # type: ignore[arg-type]
        _claimed(date(2026, 10, 1)),
    )
    assert outcome.status == "no_change"
    assert outcome.result == {"skipped": "newsroom_disabled"}
    assert not outcome.retryable


async def test_newsroom_assemble_waits_for_the_window_to_close() -> None:
    settings = Settings(newsroom_enabled=True)
    outcome = await _run_newsroom_assemble(
        settings,
        None,  # type: ignore[arg-type]
        _claimed(date(2999, 1, 1)),
    )
    assert outcome.status == "unavailable"
    assert outcome.error_code == "newsroom_window_open"
    assert outcome.retryable


def test_key_figures_are_amounts_and_rates_not_dates() -> None:
    assert key_figures("外資12日賣超521億 砍台塑3.68萬張") == {"521", "3.68"}
    assert key_figures("台積電9月營收5,118億元 第3季1.49兆元") == {"5118", "1.49"}
    assert key_figures("2026年10月15日法說會 Q3") == set()


def test_titles_below_the_bar_match_only_on_a_shared_figure() -> None:
    assert same_story_titles(0.85, "博通執行長談AI瓶頸", "博通談資本限制")
    assert same_story_titles(0.78, "外資賣超521億 加碼金控", "外資賣超521億 台塑遭砍")
    # A preview and its result differ in the figure.
    assert not same_story_titles(0.77, "中國9月出口預估年增25.5%", "中國9月出口年增36.3%")
    assert not same_story_titles(0.74, "外資賣超521億", "外資賣超521億")


def test_close_articles_make_one_story_even_when_titles_differ() -> None:
    title, other = "台積電法說會調升全年營收成長", "台積電第3季EPS創高"
    assert same_story(0.69, 0.921, title, other)
    assert same_story(0.69, 0.88, title, other)
    assert not same_story(0.69, 0.873, title, other)
    assert not same_story(0.69, None, title, other)
    # The title rule alone still merges.
    assert same_story(0.95, None, title, other)


def test_duplicate_groups_keep_known_ids_once_and_drop_singletons() -> None:
    result = DuplicateGroups(groups=[["a", "b", "a", "zzz"], ["b", "c"], ["c", "d"], ["e"]])

    assert validate_duplicate_groups(result, {"a", "b", "c", "d", "e"}) == [
        ["a", "b"],
        ["c", "d"],
    ]
