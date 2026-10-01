import uuid
from datetime import date

import pytest

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom.analysis import (
    filter_related_symbols,
    resolve_symbol,
    symbol_catalog,
)
from daily_insights_api.modules.newsroom.assembly import (
    ArticleSignal,
    EditorContractError,
    Rated,
    apply_quota,
    event_score,
    fallback_selection,
    validate_ratings,
)
from daily_insights_api.modules.newsroom.contracts import EditorRating, EditorResult
from daily_insights_api.modules.newsroom.translation import (
    load_prompt,
    to_zh_hans,
    zh_hant_digest,
)
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


def test_quota_fills_with_low_stars_only_below_five() -> None:
    rated = [
        _rated(5, 10.0),
        _rated(4, 20.0),
        _rated(3, 5.0),
        _rated(2, 80.0),
        _rated(3, 50.0),
        _rated(1, 90.0),
    ]
    selection = apply_quota(rated)
    # Two high-star events, so three fillers: stars first, then score.
    assert [(item.stars, item.score) for item in selection] == [
        (5, 10.0),
        (4, 20.0),
        (3, 50.0),
        (3, 5.0),
        (2, 80.0),
    ]


def test_quota_exactly_five_high_stars_takes_no_fillers() -> None:
    rated = [*(_rated(4, float(score)) for score in range(5)), _rated(3, 100.0)]
    assert _stars(apply_quota(rated)) == [4] * 5


def test_quota_fills_at_most_five_when_nothing_scores_high() -> None:
    rated = [_rated(stars, float(score)) for stars in (1, 2, 3) for score in range(4)]
    selection = apply_quota(rated)
    assert len(selection) == 5
    assert _stars(selection) == [3, 3, 3, 3, 2]


def test_quota_uses_all_fillers_when_fewer_than_needed() -> None:
    rated = [_rated(4, 1.0), _rated(1, 2.0)]
    assert _stars(apply_quota(rated)) == [4, 1]


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


# --- Scoring ------------------------------------------------------------------------


def test_event_score_uses_best_weighted_article_plus_source_bonus() -> None:
    source_a, source_b, source_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    signals = [
        ArticleSignal(source_a, 60, 1.5),
        ArticleSignal(source_a, 80, 1.0),
        ArticleSignal(source_b, 70, 0.5),
        ArticleSignal(source_c, 10, 2.0),
    ]
    assert event_score(signals) == 90.0 + 10.0


def test_event_score_caps_multi_source_bonus_at_twenty() -> None:
    signals = [ArticleSignal(uuid.uuid4(), 50, 1.0) for _ in range(9)]
    assert event_score(signals) == 70.0


def test_event_score_single_source_has_no_bonus() -> None:
    source = uuid.uuid4()
    assert event_score([ArticleSignal(source, 40, 1.0), ArticleSignal(source, 30, 1.0)]) == 40.0
    assert event_score([]) == 0.0


def test_fallback_takes_top_five_by_score() -> None:
    scores = [(uuid.uuid4(), float(score)) for score in range(8)]
    assert [score for _, score in fallback_selection(scores)] == [7.0, 6.0, 5.0, 4.0, 3.0]


# --- Editor contract ------------------------------------------------------------------


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


def test_zh_hant_digest_is_stable_and_content_sensitive() -> None:
    whys: dict[str, str | None] = {"b": "二", "a": "一"}
    digest = zh_hant_digest("標題", "摘要", whys)
    assert digest == zh_hant_digest("標題", "摘要", {"a": "一", "b": "二"})
    assert len(digest) == 64
    assert digest != zh_hant_digest("標題", "摘要", {"a": "一"})
    assert digest != zh_hant_digest("標題改", "摘要", whys)


@pytest.mark.parametrize("name", ["editor", "analysis", "why", "translate"])
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
