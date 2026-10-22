import asyncio
import random
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel
from sqlalchemy import event as orm_event
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.newsroom.contracts import EditorResult, TriageResult
from daily_insights_api.modules.newsroom.models import NewsroomEvent, NewsroomLlmCall
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.queue import RetryableStageError
from daily_insights_api.modules.newsroom.replay import cache as cache_module
from daily_insights_api.modules.newsroom.replay import (
    costs,
    golden,
    metrics,
    multirun,
    pipeline,
    retriage,
    runner,
    snapshot,
)
from daily_insights_api.modules.newsroom.replay.cache import (
    CachedEmbedder,
    CachedJsonModel,
    CacheStats,
    ReplayCacheMiss,
    ResponseCache,
    canonical_ids,
    replay_rep,
    request_key,
    restore_ids,
)
from daily_insights_api.modules.newsroom.replay.metrics import EventRow, MarketDay, TrackedRow
from daily_insights_api.modules.newsroom.replay.tw50 import CONSTITUENTS, match_tw50
from daily_insights_api.modules.newsroom.variants import BASELINE, P0
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


class FlakyModel(ScriptedModel):
    """Fails its first ``failures`` calls the way an invalid answer does, then answers."""

    def __init__(self, answers: Sequence[dict[str, Any]], failures: int) -> None:
        super().__init__(answers)
        self.failures = failures

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
        if self.failures:
            self.failures -= 1
            raise RetryableStageError("editor_schema_invalid")
        return await super().complete(
            database,
            model=model,
            system=system,
            payload=payload,
            result_type=result_type,
            audit=audit,
        )


async def test_an_answer_after_failed_live_calls_is_kept_for_the_next_run(
    tmp_path: Path,
) -> None:
    cache = ResponseCache(tmp_path)
    event_id = uuid.uuid4()
    live = FlakyModel([_ratings((event_id, 4))], failures=2)
    model = CachedJsonModel(cache, CacheStats(), live=lambda: live)

    async def ask(client: CachedJsonModel) -> int:
        result = await client.complete(
            _session(FakeSession()),
            model="m",
            system="s",
            payload=_editor_payload(event_id),
            result_type=EditorResult,
            audit=EDITOR_AUDIT,
        )
        return result.ratings[0].stars

    for _ in range(2):
        with pytest.raises(RetryableStageError):
            await ask(model)
    assert await ask(model) == 4
    assert await ask(CachedJsonModel(cache, CacheStats(), live=None)) == 4


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


def test_events_a_triage_replay_opens_are_named_after_their_article() -> None:
    article = uuid.uuid4()
    orm_event.listen(NewsroomEvent, "init", runner._replay_event_id)
    try:
        assert NewsroomEvent(working_title="t").id is None
        token = runner._article.set(article)
        try:
            first = NewsroomEvent(working_title="t").id
            assert first == NewsroomEvent(working_title="u").id
            assert first is not None and first != article
        finally:
            runner._article.reset(token)
    finally:
        orm_event.remove(NewsroomEvent, "init", runner._replay_event_id)


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
    reason: str = "",
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
        reason=reason,
    )


def _day(market: str = "tw_equity") -> MarketDay:
    picks = [
        _event("a", stars=5, impact=5, picked=True, tw50=["2330"], position=1),
        _event("b", stars=4, impact=2, picked=True, position=2, reason="needs a reason"),
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
    assert "| 2 | 50.0 | ★4 | 2 | 是 | title b | needs a reason |" in text


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
    assert "## p0/score: triage impact 與星等差距 ≥ 2 級 (2 則)" in text
    assert "| 2026-10-20 | tw_equity | 2 | 50.0 | ★4 | 2 | 是 | title b | needs a reason |" in text


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


def test_rescoring_follows_the_variant() -> None:
    result = pipeline.triage_result(
        topic="economy",
        story_type="data",
        signals={
            "impact": 5,
            "new_facts": False,
            "directness": {"global": 4, "tw_equity": 2, "us_equity": 0},
        },
    )
    assert result is not None and result.escalation == 0
    assert pipeline.market_scores(result, variant=BASELINE) == {
        "global": 81,
        "tw_equity": 52,
        "us_equity": 0,
    }
    assert pipeline.market_scores(result, variant=P0)["global"] == 85
    assert pipeline.triage_result(topic=None, story_type="data", signals={"impact": 3}) is None


# --- Repetitions and costs ---------------------------------------------------------------


def test_repetition_one_keeps_the_key_earlier_runs_stored() -> None:
    def key(**rep: int) -> str:
        return request_key(model="m", system="s", payload={"a": 1}, subject=None, **rep)[0]

    assert key(rep=1) == key()
    assert key(rep=2) not in {key(), key(rep=3)}


async def test_a_repetition_draws_its_own_answer_and_keeps_it(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    event_id = uuid.uuid4()
    live = ScriptedModel([_ratings((event_id, 3)), _ratings((event_id, 4))])

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

    assert await ask(CachedJsonModel(cache, CacheStats(), live=lambda: live)) == 3
    with replay_rep(2):
        assert await ask(CachedJsonModel(cache, CacheStats(), live=lambda: live)) == 4
    offline = CachedJsonModel(cache, CacheStats(), live=None)
    assert await ask(offline) == 3
    with replay_rep(2):
        assert await ask(CachedJsonModel(cache, CacheStats(), live=None)) == 4
    with replay_rep(3), pytest.raises(ReplayCacheMiss):
        await ask(CachedJsonModel(cache, CacheStats(), live=None))


class UsageModel(ScriptedModel):
    """Reports provider usage the way the replay's response hook does."""

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
        sink = cache_module._usage_sink.get()
        assert sink is not None
        sink.append(
            {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "prompt_cache_hit_tokens": 60,
                "model": "deepseek-chat",
            }
        )
        return await super().complete(
            database,
            model=model,
            system=system,
            payload=payload,
            result_type=result_type,
            audit=audit,
        )


async def test_a_live_answer_is_priced_into_the_round_ledger(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "cache")
    ledger = costs.Ledger(tmp_path / "ledger" / "p0b.jsonl", "run-1")
    stats = CacheStats()
    event_id = uuid.uuid4()
    live = UsageModel([_ratings((event_id, 3))])
    model = CachedJsonModel(cache, stats, live=lambda: live, ledger=ledger)
    await model.complete(
        _session(FakeSession()),
        model="deepseek-chat",
        system="s",
        payload=_editor_payload(event_id),
        result_type=EditorResult,
        audit=EDITOR_AUDIT,
    )
    [entry] = costs.read_ledger(ledger.path)
    assert (entry.input_tokens, entry.cache_hit_tokens, entry.output_tokens) == (100, 60, 10)
    assert entry.served_model == "deepseek-chat"
    assert stats.count(EDITOR_AUDIT).live_usd == pytest.approx(entry.usd)
    assert stats.count(EDITOR_AUDIT).live_cache_hit_tokens == 60
    key, _ = request_key(
        model="deepseek-chat", system="s", payload=_editor_payload(event_id), subject=None
    )
    stored = cache.answer(key, 0)
    assert stored is not None and stored.cache_hit_tokens == 60
    assert costs.ledger_total([entry])["usd"] == pytest.approx(entry.usd)


def test_prices_follow_the_cache_split_and_the_peak_hours() -> None:
    peak = datetime(2026, 10, 22, 7, tzinfo=UTC)  # Thursday 07:00 UTC
    off_peak = datetime(2026, 10, 22, 12, tzinfo=UTC)
    weekend = datetime(2026, 10, 24, 7, tzinfo=UTC)
    assert costs.is_peak(peak) and not costs.is_peak(off_peak) and not costs.is_peak(weekend)
    usd = costs.chat_cost(
        "deepseek-chat",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_hit_tokens=500_000,
        at=peak,
    )
    assert usd == pytest.approx(0.5 * 0.006 + 0.5 * 0.3 + 1.2)
    assert costs.chat_cost(
        "deepseek-chat", input_tokens=1_000_000, output_tokens=0, cache_hit_tokens=None, at=off_peak
    ) == pytest.approx(0.15)
    assert costs.chat_cost(
        "deepseek-v4-pro", input_tokens=0, output_tokens=1_000_000, cache_hit_tokens=0, at=peak
    ) == pytest.approx(3.96)


# --- Targeted re-triage ------------------------------------------------------------------


def _judged(
    title: str,
    *,
    impact: int,
    relevant: bool = True,
    topic: str = "economy",
    escalation: int = 0,
) -> retriage.Judged:
    return retriage.Judged(
        article_id=uuid.uuid5(uuid.NAMESPACE_URL, title),
        edition_date=date(2026, 10, 20),
        title=title,
        topic=topic,
        story_type="other",
        signals={
            "impact": impact,
            "escalation": escalation,
            "new_facts": True,
            "directness": {"global": 3, "tw_equity": 2, "us_equity": 2},
        },
        relevant=relevant,
        event_id=uuid.uuid4(),
    )


def test_retriage_targets_energy_and_shipping_and_samples_a_control() -> None:
    articles = [
        _judged("伊朗攻擊荷姆茲海峽貨櫃船", impact=5),
        _judged("Brent jumps on supply fears", impact=3),
        _judged("OPEC+ meeting", impact=4, topic="commodities"),
        _judged("以色列總理談話", impact=3, escalation=1),
        _judged("油價小幅波動", impact=2),
        _judged("油輪遇襲", impact=4, relevant=False),
        *(_judged(f"聯準會決議 {n}", impact=4 + n % 2) for n in range(5)),
        _judged("台積電法說", impact=3),
    ]
    targets, controls = retriage.select(articles, retriage.Selection(control_sample=3))
    assert [article.title for article in targets] == [
        "伊朗攻擊荷姆茲海峽貨櫃船",
        "Brent jumps on supply fears",
        "OPEC+ meeting",
        "以色列總理談話",
    ]
    assert len(controls) == 3 and all(a.title.startswith("聯準會決議") for a in controls)
    again = retriage.select(reversed(articles), retriage.Selection(control_sample=3))[1]
    assert [a.article_id for a in again] == [a.article_id for a in controls]


def test_a_retriaged_article_keeps_its_topic_and_directness() -> None:
    base = TriageResult.model_validate(
        {
            "topic": "policy",
            "story_type": "official",
            "new_facts": True,
            "impact": 5,
            "escalation": 2,
            "directness": {"global": 3, "tw_equity": 1, "us_equity": 2},
        }
    )
    answer = TriageResult.model_validate(
        {
            "topic": "commodities",
            "story_type": "opinion",
            "new_facts": False,
            "impact": 3,
            "escalation": 0,
            "directness": {"global": 1, "tw_equity": 1, "us_equity": 1},
            "event_date": "2026-10-19",
        }
    )
    merged = retriage.merged_result(base, answer)
    assert (merged.topic, merged.directness) == (base.topic, base.directness)
    assert (merged.impact, merged.escalation, merged.story_type, merged.new_facts) == (
        3,
        0,
        "opinion",
        False,
    )
    assert merged.event_date == date(2026, 10, 19)


async def test_the_recording_model_keeps_each_triage_request() -> None:
    inner = ScriptedModel([_ratings((uuid.uuid4(), 3))])
    recorder = retriage.RecordingModel(inner)
    await recorder.complete(
        _session(FakeSession()),
        model="m",
        system="prompt",
        payload={"article": {"title": "x"}},
        result_type=EditorResult,
        audit=TRIAGE_AUDIT,
    )
    assert recorder.requests[uuid.UUID(int=7)] == retriage.Request(
        "prompt", {"article": {"title": "x"}}, "m"
    )


def test_a_retriage_estimate_prices_the_shared_prompt_as_cached() -> None:
    estimate = retriage.Estimate()
    estimate.add(
        date(2026, 10, 20),
        [retriage.Request("s", {"a": 1}, "m")] * 2,
        system="x" * 1000,
        tokens_per_char=1.0,
        output_per_call=100,
    )
    assert estimate.calls == 2 and estimate.per_day == {"2026-10-20": 2}
    assert estimate.system_tokens == 2000
    assert estimate.usd() < estimate.usd(system_cached=False)


# --- Invalid days, golden set and repeated runs -----------------------------------------


def test_a_fallback_day_is_invalid_and_left_out_of_comparisons() -> None:
    data = _day().as_dict()
    data.pop("invalid_reason")
    data["selection_mode"] = "fallback"
    day = MarketDay.load(data)
    assert not day.valid and day.invalid_reason == metrics.FALLBACK_REASON
    meta = {
        "start": "2026-10-20",
        "end": "2026-10-20",
        "variant": "baseline",
        "stages": "triage",
        "started_at": "now",
        "snapshot": "x",
        "run_dir": "/a",
    }
    assert "| 2026-10-20 | tw_equity | fallback | " + metrics.FALLBACK_REASON in metrics.summary(
        meta, [day]
    )
    text = metrics.compare(meta, [day], meta, [_day()])
    assert "作廢而不比較的市場日" in text
    assert "| 2026-10-20 | tw_equity | 1 |" not in text


def test_the_golden_set_draft_waits_for_the_editors() -> None:
    items = golden.load()
    assert [item.id for item in items] == [f"G{n}" for n in range(1, 8)]
    assert {item.status for item in items} == {"待編輯確認"}
    assert {item.expect for item in items} == {"eq 5", "gte 4", "lt 5"}


def _golden_item(expect: str, *keywords: str, exclude: tuple[str, ...] = ()) -> golden.GoldenItem:
    return golden.GoldenItem(
        id="x",
        description="d",
        market="tw_equity",
        start=date(2026, 10, 20),
        end=date(2026, 10, 20),
        keywords=keywords,
        exclude=exclude,
        expect=expect,
        annotator="a",
        status="待編輯確認",
    )


def test_golden_items_judge_the_best_or_the_worst_matching_event() -> None:
    day = _day()
    day.candidates[1].article_titles = ["Iran attacks container ships", "其他"]
    assert golden.evaluate(_golden_item("eq 5", "title a"), [day]).passed is True
    reach = golden.evaluate(_golden_item("eq 5", "Iran", "container"), [day])
    assert (reach.passed, reach.stars, len(reach.matches)) == (False, 4, 1)
    below = golden.evaluate(_golden_item("lt 5", "title [ab]"), [day])
    assert (below.passed, below.stars) == (False, 5)
    excluded = golden.evaluate(_golden_item("lt 5", "title [ab]", exclude=("title a",)), [day])
    assert excluded.passed is True
    missing = golden.evaluate(_golden_item("gte 4", "nothing"), [day])
    assert (missing.passed, missing.stars) == (False, None)
    assert golden.evaluate(_golden_item("lt 5", "nothing"), [day]).passed is True
    day.invalid_reason = "fallback"
    assert golden.evaluate(_golden_item("eq 5", "title a"), [day]).passed is None


def test_slot_changes_are_classified_by_their_star_boundary() -> None:
    left, right = _day(), _day()
    # a: 5 vs 4 but kept; b: ★4 left, ★3 right; c: ★3 on both sides, lost the fill.
    right.candidates[0].stars = 4
    right.candidates[1].stars = 3
    right.picks = [right.candidates[0], _event("x", stars=4, picked=True)]
    diff = multirun.slot_diff(left, right, multirun.event_key)
    assert (diff.only_left, diff.only_right, diff.changed) == (2, 1, 2)
    assert diff.boundaries == {
        "4/3 星邊界": 1,
        "3 星補位": 1,
        "對方未評 (不在前 40、前版排除或分群不同)": 1,
    }
    assert diff.star_flips == {"5/4": 1, "4/3": 1}


def _run(name: str, *days: MarketDay) -> multirun.Run:
    return multirun.Run(name, {}, list(days))


def test_a_report_flags_only_differences_beyond_the_baseline_noise() -> None:
    base_days = [_day(), _day(), _day()]
    base_days[2].picks = base_days[2].picks[:2]
    variant_days = [_day(), _day(), _day()]
    for day in variant_days:
        for row in day.candidates:
            if row.event_id == "c":
                row.stars = 5
        day.picks = [*day.picks, _event("n", stars=5, picked=True)]
        day.candidates.append(day.picks[-1])
    groups = [
        multirun.Group("baseline", [_run(f"b{n}", day) for n, day in enumerate(base_days)]),
        multirun.Group("p0b", [_run(f"p{n}", day) for n, day in enumerate(variant_days)]),
    ]
    text = multirun.report(groups, baseline="baseline", items=golden.load())
    assert "| 2026-10-20 | tw_equity | 1/1/1 | 3/3/3 **實際差異** |" in text
    assert "3/3 ★5 title n **實際差異: baseline 一次都沒入選**" in text
    assert "- 3/3 ★5 title a" in text
    assert "| baseline 組內 (兩兩) | 3 | 0, 1, 1 |" in text
    assert "## 金標準逐項結果" in text


# --- Command line, new options -----------------------------------------------------------


def test_a_retriage_run_needs_its_base_and_parses_skipped_days() -> None:
    options = run_newsroom_replay.parse_args(
        [
            "run",
            "--stages",
            "retriage",
            "--variant",
            "p0b",
            "--base-variant",
            "p0",
            "--rep",
            "2",
            "--skip-day",
            "2026-10-13=斷線, 為省成本不補跑",
            "--dry-run",
        ]
    )
    assert options.skip_days == ((date(2026, 10, 13), "斷線, 為省成本不補跑"),)
    assert (options.rep, options.triage_rep, options.base_variant) == (2, 1, "p0")
    assert run_newsroom_replay._selection(options) == retriage.Selection()
    for wrong in (
        ["run", "--stages", "retriage"],
        ["run", "--dry-run"],
        ["run", "--rep", "0"],
        ["run", "--skip-day", "2026-10-13"],
    ):
        with pytest.raises(SystemExit):
            run_newsroom_replay.parse_args(wrong)


def test_a_longer_repeat_lookback_gets_its_own_snapshot_with_published_vectors() -> None:
    short = snapshot.Snapshot(date(2026, 10, 18), date(2026, 10, 22))
    long = snapshot.Snapshot(date(2026, 10, 18), date(2026, 10, 22), lookback_days=14)
    assert (short.name, short.template) == ("20261018-20261022", "replay_20261018_20261022")
    assert (long.name, long.template) == (
        "20261018-20261022-lb14",
        "replay_20261018_20261022_lb14",
    )
    assert long.history_start == date(2026, 10, 4)
    filters = snapshot._filters(long)
    assert "edition_date >= '2026-10-04'" in filters["newsroom_editions"]
    assert (
        "event_id IN (SELECT event_id FROM newsroom_edition_items" in filters["newsroom_articles"]
    )
    assert "id IN (SELECT event_id FROM newsroom_edition_items" in filters["newsroom_events"]
    body = snapshot._select_expression("newsroom_articles", "body", long)
    assert body.startswith("CASE WHEN edition_date >= '2026-10-18' THEN left(body, 1500)")
    assert snapshot._select_expression("newsroom_articles", "title", long) == '"title"'


def test_the_snapshot_reaches_back_as_far_as_the_variants_read() -> None:
    def lookback(*args: str) -> int:
        options = run_newsroom_replay.parse_args(["run", *args])
        return run_newsroom_replay._snapshot(options).lookback_days

    assert lookback("--variant", "p0b") == 2
    assert lookback("--variant", "oldnews") == 14
    assert lookback("--stages", "retriage", "--variant", "oldnews", "--base-variant", "p0b") == 14
    assert lookback("--variant", "oldnews", "--lookback-days", "3") == 3


def test_a_stored_event_date_is_rebuilt_with_the_judgement() -> None:
    result = pipeline.triage_result(
        topic="economy",
        story_type="data",
        signals={
            "impact": 4,
            "new_facts": True,
            "directness": {"global": 3, "tw_equity": 2, "us_equity": 3},
            "event_date": "2026-10-02",
        },
    )
    assert result is not None and result.event_date == date(2026, 10, 2)
