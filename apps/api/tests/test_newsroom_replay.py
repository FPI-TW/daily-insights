import asyncio
import random
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom.contracts import EditorResult, TriageResult
from daily_insights_api.modules.newsroom.models import NewsroomLlmCall
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.replay import metrics, runner, snapshot
from daily_insights_api.modules.newsroom.replay.cache import (
    CachedEmbedder,
    CachedJsonModel,
    CacheStats,
    ReplayCacheMiss,
    ResponseCache,
    canonical_ids,
    request_key,
    restore_ids,
)
from daily_insights_api.modules.newsroom.replay.metrics import EventRow, MarketDay, TrackedRow
from daily_insights_api.modules.newsroom.replay.tw50 import CONSTITUENTS, match_tw50
from daily_insights_api.scripts import run_newsroom_replay

TRIAGE_AUDIT = CallAudit("triage", uuid.UUID(int=7), "triage-v2:abc")
EDITOR_AUDIT = CallAudit("editor", None, "newsroom.editor.v3")


class FakeSession:
    """Just what the cached clients touch: ``add`` and the pending ``new`` rows."""

    def __init__(self) -> None:
        self.new: list[Any] = []

    def add(self, row: Any) -> None:
        self.new.append(row)


def _session(fake: FakeSession) -> AsyncSession:
    return cast(AsyncSession, fake)


class ScriptedModel:
    """A provider that answers from a script and writes an audit row like the real one."""

    def __init__(self, answers: Sequence[dict[str, Any]]) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def complete[R: BaseModel](
        self,
        database: AsyncSession,
        *,
        model: str,
        system: str,
        payload: dict[str, Any],
        result_type: type[R],
        audit: CallAudit,
    ) -> R:
        self.calls.append(payload)
        database.add(
            NewsroomLlmCall(
                stage=audit.stage,
                model=model,
                input_tokens=100,
                output_tokens=10,
                latency_ms=1,
            )
        )
        return result_type.model_validate(self.answers.pop(0))


def _editor_payload(*event_ids: uuid.UUID) -> dict[str, Any]:
    return {
        "market": "global",
        "events": [{"event_id": str(event_id), "working_title": "t"} for event_id in event_ids],
    }


def _ratings(*pairs: tuple[uuid.UUID, int]) -> dict[str, Any]:
    return {"ratings": [{"event_id": str(event_id), "stars": stars} for event_id, stars in pairs]}


# --- Taiwan 50 -------------------------------------------------------------------------


def test_the_taiwan_50_list_has_fifty_distinct_constituents() -> None:
    assert len({constituent.code for constituent in CONSTITUENTS}) == 50


@pytest.mark.parametrize(
    ("title", "codes"),
    [
        ("台積電法說會釋出第四季財測", ["2330"]),
        ("TSMC raises capex", ["2330"]),
        ("日月光投控與聯發科合作", ["2454", "3711"]),
        ("南亞塑膠調漲報價", ["1303"]),
        ("南亞科DRAM合約價上揚", ["2408"]),
        ("公平會不禁止台灣大公開收購精誠", ["3045"]),
        ("京元電擴產", ["2449"]),
        ("聯茂執行長看好高階CCL", []),
    ],
)
def test_constituents_are_matched_by_name_alias_and_code(title: str, codes: list[str]) -> None:
    assert match_tw50(title) == codes


@pytest.mark.parametrize(
    "title",
    ["南亞地區需求疲弱", "政府統一口徑說明", "台灣大跌逾千點", "長榮航第三季獲利", "營收2330億元"],
)
def test_everyday_words_and_amounts_are_not_constituents(title: str) -> None:
    assert match_tw50(title) == []


def test_a_code_in_a_title_matches() -> None:
    assert match_tw50("力積電(6770)與鴻海(2317)簽約") == ["2317"]


# --- Response cache --------------------------------------------------------------------


def test_event_ids_are_replaced_in_order_and_restored() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    text, ids = canonical_ids(f'{{"a":"{first}","b":"{second}","c":"{first}"}}')
    assert text == '{"a":"<<id:0>>","b":"<<id:1>>","c":"<<id:0>>"}'
    assert ids == [str(first), str(second)]
    assert restore_ids(text, ids) == f'{{"a":"{first}","b":"{second}","c":"{first}"}}'


def test_the_same_request_with_new_event_ids_has_the_same_key() -> None:
    def key(*event_ids: uuid.UUID) -> str:
        return request_key(
            model="m", system="s", payload=_editor_payload(*event_ids), subject=None
        )[0]

    assert key(uuid.uuid4(), uuid.uuid4()) == key(uuid.uuid4(), uuid.uuid4())
    assert key(uuid.uuid4()) != key(uuid.uuid4(), uuid.uuid4())
    assert (
        request_key(model="m", system="s", payload={}, subject=uuid.UUID(int=1))[0]
        != request_key(model="m", system="s", payload={}, subject=uuid.UUID(int=2))[0]
    )


async def test_a_second_run_is_answered_from_the_cache_with_its_own_event_ids(
    tmp_path: Path,
) -> None:
    cache = ResponseCache(tmp_path)
    first_ids = (uuid.uuid4(), uuid.uuid4())
    live = ScriptedModel([_ratings((first_ids[0], 5), (first_ids[1], 2))])
    stats = CacheStats()
    model = CachedJsonModel(cache, stats, live=lambda: live)
    first = await model.complete(
        _session(FakeSession()),
        model="m",
        system="s",
        payload=_editor_payload(*first_ids),
        result_type=EditorResult,
        audit=EDITOR_AUDIT,
    )
    assert [rating.stars for rating in first.ratings] == [5, 2]
    assert stats.count(EDITOR_AUDIT).live_input_tokens == 100

    second_ids = (uuid.uuid4(), uuid.uuid4())
    rerun_stats = CacheStats()
    session = FakeSession()
    offline = CachedJsonModel(cache, rerun_stats, live=None)
    again = await offline.complete(
        _session(session),
        model="m",
        system="s",
        payload=_editor_payload(*second_ids),
        result_type=EditorResult,
        audit=EDITOR_AUDIT,
    )
    assert [(rating.event_id, rating.stars) for rating in again.ratings] == [
        (str(second_ids[0]), 5),
        (str(second_ids[1]), 2),
    ]
    count = rerun_stats.count(EDITOR_AUDIT)
    assert (count.calls, count.hits, count.input_tokens, count.live_input_tokens) == (1, 1, 100, 0)
    assert len(live.calls) == 1
    assert [row.request_id for row in session.new] == ["replay-cache"]


async def test_a_retried_request_replays_its_answers_in_order(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    event_id = uuid.uuid4()
    live = ScriptedModel([_ratings((event_id, 1)), _ratings((event_id, 4))])

    async def ask(model: CachedJsonModel) -> int:
        result = await model.complete(
            _session(FakeSession()),
            model="m",
            system="s",
            payload=_editor_payload(event_id),
            result_type=EditorResult,
            audit=EDITOR_AUDIT,
        )
        return result.ratings[0].stars

    first_run = CachedJsonModel(cache, CacheStats(), live=lambda: live)
    assert [await ask(first_run), await ask(first_run)] == [1, 4]
    second_run = CachedJsonModel(cache, CacheStats(), live=None)
    assert [await ask(second_run), await ask(second_run)] == [1, 4]
    with pytest.raises(ReplayCacheMiss):
        await ask(second_run)


async def test_an_offline_miss_never_reaches_a_provider(tmp_path: Path) -> None:
    model = CachedJsonModel(ResponseCache(tmp_path), CacheStats(), live=None)
    with pytest.raises(ReplayCacheMiss):
        await model.complete(
            _session(FakeSession()),
            model="m",
            system="s",
            payload={"article": {"title": "x"}, "candidate_events": []},
            result_type=TriageResult,
            audit=TRIAGE_AUDIT,
        )


class ScriptedEmbedder:
    def __init__(self) -> None:
        self.requests: list[list[str]] = []

    async def embed(
        self, database: AsyncSession, texts: Sequence[str], *, audit: CallAudit
    ) -> list[list[float]]:
        self.requests.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]


async def test_titles_are_embedded_once_and_then_served_from_the_cache(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    live = ScriptedEmbedder()
    stats = CacheStats()
    embedder = CachedEmbedder(cache, stats, model="e", live=lambda: live)
    audit = CallAudit("embed", None)
    assert await embedder.embed(_session(FakeSession()), ["ab", "c"], audit=audit) == [
        [2.0, 1.0],
        [1.0, 1.0],
    ]
    assert await embedder.embed(_session(FakeSession()), ["c", "ab", "def"], audit=audit) == [
        [1.0, 1.0],
        [2.0, 1.0],
        [3.0, 1.0],
    ]
    assert live.requests == [["ab", "c"], ["def"]]
    assert (stats.embed.calls, stats.embed.hits) == (5, 2)


# --- Deterministic triage order -------------------------------------------------------


@pytest.mark.parametrize("window", [1, 3, 8])
async def test_each_article_sees_exactly_the_articles_a_window_before_it(window: int) -> None:
    total = 25
    gate = runner.TriageGate(total, window)
    committed: list[int] = []
    seen: dict[int, list[int]] = {}
    delays = random.Random(window)

    async def member(index: int) -> None:
        runner._member.set(index)
        await gate.wait_start(index)
        seen[index] = list(committed)
        await gate.arrive()
        await asyncio.sleep(delays.random() / 200)
        await gate.wait_turn()
        committed.append(index)
        await gate.finish(index)

    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        for index in range(total):
            group.create_task(member(index))
    assert committed == list(range(total))
    assert all(seen[index] == list(range(max(0, index - window + 1))) for index in range(total))


async def test_a_member_that_never_calls_the_model_does_not_block_the_others() -> None:
    gate = runner.TriageGate(3, 3)
    order: list[int] = []

    async def member(index: int, *, calls: bool) -> None:
        runner._member.set(index)
        await gate.wait_start(index)
        if calls:
            await gate.arrive()
            await gate.wait_turn()
        order.append(index)
        await gate.finish(index)

    async with asyncio.timeout(5), asyncio.TaskGroup() as group:
        group.create_task(member(0, calls=True))
        group.create_task(member(1, calls=False))
        group.create_task(member(2, calls=True))
    assert order.index(0) < order.index(2)


def test_story_groups_are_rebuilt_from_the_window_decisions() -> None:
    target, merged, kept, other_target, other = (str(uuid.UUID(int=n)) for n in range(1, 6))
    decisions = [
        {"matched_event_id": target, "event_id": merged, "outcome": "merged"},
        {"matched_event_id": target, "event_id": kept, "outcome": "below_threshold"},
        {"matched_event_id": other_target, "event_id": other, "outcome": "merged"},
    ]
    groups = runner._story_groups(decisions)
    assert set(groups) == {uuid.UUID(target), uuid.UUID(kept)}
    assert groups[uuid.UUID(target)] == groups[uuid.UUID(kept)]


# --- Metrics and Markdown --------------------------------------------------------------


def _event(
    name: str,
    *,
    stars: int | None,
    impact: int | None = 3,
    picked: bool = False,
    topic: str = "companies",
    tw50: list[str] | None = None,
    position: int = 1,
) -> EventRow:
    return EventRow(
        event_id=name,
        title=f"title {name}",
        position=position,
        score=50.0,
        stars=stars,
        picked=picked,
        rank=position if picked else None,
        impact=impact,
        topic=topic,
        prod_event_ids=[name],
        tw50=tw50 or [],
    )


def _day(market: str = "tw_equity") -> MarketDay:
    picks = [
        _event("a", stars=5, impact=5, picked=True, tw50=["2330"], position=1),
        _event("b", stars=4, impact=2, picked=True, position=2),
        _event("c", stars=3, impact=3, picked=True, topic="economy", position=3),
    ]
    return MarketDay(
        edition_date="2026-10-20",
        market=market,
        selection_mode="editor",
        candidates=[*picks, _event("d", stars=1, impact=4, position=4), _event("e", stars=None)],
        picks=picks,
        tracked=[TrackedRow("聯茂擴產", _event("b", stars=4, picked=True, position=2))],
        reference_picks=[_event("a", stars=4, picked=True), _event("z", stars=4, picked=True)],
        articles=10,
        relevant=4,
        market_relevant=3,
    )


def test_market_day_metrics() -> None:
    day = _day()
    assert day.five_stars == 1
    assert day.star_distribution() == {"5": 1, "4": 1, "3": 1, "2": 0, "1": 1, "unrated": 1}
    assert [row.event_id for row in day.impact_gaps()] == ["b", "d"]
    assert day.tw50_share() == {
        "picks": 3,
        "company_stories": 2,
        "non_tw50_company_stories": 1,
        "share": 1 / 3,
    }
    assert day.reference_overlap() == {"same": 1, "replay_only": 2, "production_only": 1}
    day.reference_stars = {"a": 4, "b": 4, "z": 3}
    assert day.star_agreement() == {"shared": 2, "same": 1, "higher": 1, "lower": 0}


def test_a_market_day_survives_its_json() -> None:
    day = _day()
    assert MarketDay.load(day.as_dict()) == day


def test_the_summary_lists_the_overview_edition_and_tracking() -> None:
    meta = {
        "start": "2026-10-20",
        "end": "2026-10-20",
        "variant": "baseline",
        "stages": "editor",
        "started_at": "now",
        "snapshot": "20261020-20261020",
        "notes": ["note one"],
        "day_notes": {"2026-10-20": ["rescored"]},
        "cache": {
            "llm": {
                "editor x": {
                    "calls": 2,
                    "hits": 1,
                    "misses": 1,
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "live_input_tokens": 5,
                    "live_output_tokens": 1,
                }
            }
        },
    }
    text = metrics.summary(meta, [_day()])
    assert (
        "| 2026-10-20 | tw_equity | editor | 1 | 1/1/1/0/1 (+1 未評) | 3 | 1 | 2 | 1 | 40% |"
        in text
    )
    assert "| 聯茂擴產 | 2026-10-20 | tw_equity | 2 |" in text
    assert "| editor x | 2 | 1 | 1 | 10 | 2 | 5 | 1 |" in text
    assert "- 2026-10-20: rescored" in text
    assert "### triage impact 與星等差距 ≥ 2 級 (2 則)" in text


def test_compare_shows_both_runs_side_by_side() -> None:
    left = _day()
    right = _day()
    right.candidates[0].stars = 4
    right.picks = [right.picks[0], _event("x", stars=4, picked=True)]
    text = metrics.compare(
        {"variant": "baseline", "stages": "score", "run_dir": "/a"},
        [left],
        {"variant": "p0", "stages": "score", "run_dir": "/b"},
        [right],
    )
    assert "| 2026-10-20 | tw_equity | 1 | 0 |" in text
    assert "| 僅左 | ★3 |" in text
    assert "| 僅右 | — | — | ★4 |" in text
    assert "| 聯茂擴產 | 2026-10-20 | tw_equity | title b |" in text


def test_tracks_are_parsed_from_the_command_line() -> None:
    track = metrics.parse_track("台積電=台積電|TSMC@tw_equity,global")
    assert track == metrics.Track("台積電", ("tw_equity", "global"), "台積電|TSMC")
    assert metrics.parse_track("Fed=聯準會").markets == ("global", "tw_equity", "us_equity")
    with pytest.raises(ValueError):
        metrics.parse_track("no pattern")
    with pytest.raises(ValueError):
        metrics.parse_track("x=y@nowhere")


def test_default_tracks_match_the_stories_under_review() -> None:
    labels = {
        title: [track.label for track in metrics.DEFAULT_TRACKS if track.matches([title])]
        for title in (
            "伊朗在荷姆茲海峽攻擊至少15艘貨櫃船",
            "沙烏地阿美謝巴油田遭無人機攻擊",
            "Meta公布第三季財報",
            "特斯拉公布第三季財報 獲利優於預期",
            "聯茂執行長看好高階CCL",
        )
    }
    assert labels == {
        "伊朗在荷姆茲海峽攻擊至少15艘貨櫃船": ["荷姆茲攻擊/伊朗攻擊貨櫃船"],
        "沙烏地阿美謝巴油田遭無人機攻擊": ["沙烏地阿美油田遇襲"],
        "Meta公布第三季財報": ["Meta 財報"],
        "特斯拉公布第三季財報 獲利優於預期": ["特斯拉財報"],
        "聯茂執行長看好高階CCL": ["聯茂擴產"],
    }


# --- Command line ------------------------------------------------------------------------


def test_the_script_defaults_to_the_review_window() -> None:
    options = run_newsroom_replay.parse_args(["run"])
    assert (options.start.isoformat(), options.end.isoformat()) == ("2026-10-18", "2026-10-22")
    assert (options.variant, options.stages, options.concurrency) == ("baseline", "score", 6)
    assert options.tracks == metrics.DEFAULT_TRACKS
    with pytest.raises(SystemExit):
        run_newsroom_replay.parse_args(["run", "--concurrency", "9"])
    with pytest.raises(SystemExit):
        run_newsroom_replay.parse_args(["run", "--start", "2026-10-22", "--end", "2026-10-18"])


def test_the_script_refuses_a_variant_this_branch_cannot_score(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def unexpected(*_: Any) -> None:
        raise AssertionError("no snapshot before the variant check")

    monkeypatch.setattr(snapshot, "prepare", unexpected)
    code = run_newsroom_replay.main(["--root", str(tmp_path), "run", "--variant", "p0"])
    assert code == 1
    assert "variant 'p0'" in capsys.readouterr().err
