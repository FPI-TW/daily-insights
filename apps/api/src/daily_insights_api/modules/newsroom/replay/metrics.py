"""What a replay measures per market and day, and the Markdown it writes.

Everything here is pure: the runner fills ``MarketDay`` records from the replay
database, ``summary`` renders one run and ``compare`` two runs, both from the
JSON files a run leaves behind.
"""

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from daily_insights_api.modules.newsroom.models import MARKET_CODES

GAP_LEVELS = 2
TOP_SCORES = 20
STAR_LEVELS = (5, 4, 3, 2, 1)
COMPANY_TOPIC = "companies"


@dataclass(frozen=True, slots=True)
class Track:
    """A story followed across runs by a title pattern, in the markets it matters to."""

    label: str
    markets: tuple[str, ...]
    pattern: str

    def matches(self, texts: Iterable[str]) -> bool:
        expression = re.compile(self.pattern, re.IGNORECASE)
        return any(expression.search(text) for text in texts if text)


# task.md "驗證": the stories the P0 recalibration is judged on.
DEFAULT_TRACKS = (
    Track("聯茂擴產", ("tw_equity", "us_equity", "global"), r"聯茂"),
    Track(
        "荷姆茲攻擊/伊朗攻擊貨櫃船",
        ("global", "tw_equity"),
        r"荷姆茲|霍爾木茲|Hormuz|伊朗.{0,15}(貨櫃|商船|油輪|船隻|航運)",
    ),
    Track(
        "Meta 財報",
        ("us_equity",),
        r"(Meta|臉書|祖克柏).{0,20}(財報|營收|獲利|財測|資本支出|季度|第[一二三四]季)",
    ),
    Track(
        "特斯拉財報",
        ("us_equity",),
        r"(特斯拉|Tesla).{0,20}(財報|營收|獲利|財測|毛利|季度|第[一二三四]季)",
    ),
    Track("沙烏地阿美油田遇襲", ("global",), r"沙烏地阿美|阿美.{0,6}油田|Aramco|謝巴|Shaybah"),
)


def parse_track(text: str) -> Track:
    """``LABEL=REGEX`` or ``LABEL=REGEX@market,market`` (all markets by default)."""
    label, separator, rest = text.partition("=")
    if not separator or not label.strip() or not rest.strip():
        raise ValueError("a track is LABEL=REGEX[@market,...]")
    pattern, _, markets = rest.rpartition("@") if "@" in rest else (rest, "", "")
    chosen = tuple(market.strip() for market in markets.split(",") if market.strip())
    if any(market not in MARKET_CODES for market in chosen):
        raise ValueError(f"unknown market in {text!r}")
    re.compile(pattern)
    return Track(label.strip(), chosen or MARKET_CODES, pattern)


@dataclass(slots=True)
class EventRow:
    """One event of one market's ranking, with what the replay decided about it."""

    event_id: str
    title: str
    # 1-based place in the market's ranked list; the editor sees the first 40.
    position: int | None
    score: float | None
    stars: int | None
    picked: bool = False
    # 1-based place in the edition when picked.
    rank: int | None = None
    follow_up: bool = False
    # The editor's one-sentence reason (editor prompt v4 on).
    reason: str = ""
    # Highest triage impact and escalation among the event's relevant reports.
    impact: int | None = None
    escalation: int | None = None
    topic: str | None = None
    headlines: list[str] = field(default_factory=list)
    # Every relevant report's title (runs before this field have only ``headlines``).
    article_titles: list[str] = field(default_factory=list)
    # Production events this one stands for: itself, or after a triage replay
    # the production events most of its articles belonged to.
    prod_event_ids: list[str] = field(default_factory=list)
    tw50: list[str] = field(default_factory=list)

    @property
    def company_story(self) -> bool:
        return self.topic == COMPANY_TOPIC

    def texts(self) -> list[str]:
        """The working title and every report title known for the event."""
        return [self.title, *(self.article_titles or self.headlines)]

    @classmethod
    def load(cls, data: dict[str, Any]) -> "EventRow":
        return cls(**data)


@dataclass(slots=True)
class TrackedRow:
    label: str
    event: EventRow

    @classmethod
    def load(cls, data: dict[str, Any]) -> "TrackedRow":
        return cls(data["label"], EventRow.load(data["event"]))


@dataclass(slots=True)
class MarketDay:
    edition_date: str
    market: str
    # ``editor`` or ``fallback`` as assembly decided; ``stored`` when the score
    # replay reused production's stars.
    selection_mode: str
    candidates: list[EventRow]
    picks: list[EventRow]
    tracked: list[TrackedRow] = field(default_factory=list)
    reference_picks: list[EventRow] = field(default_factory=list)
    # Production's stars for its own candidates of the day, by event id.
    reference_stars: dict[str, int] = field(default_factory=dict)
    # Window articles, the relevant ones, and those at or above the relevance
    # bar in this market.
    articles: int = 0
    relevant: int = 0
    market_relevant: int = 0
    five_star_review: bool = False
    # Score replay only: whether production's list and edition came out again.
    reproduction: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    # Set when the day must not be compared (the editor fell back to scores,
    # the day was skipped, or triage failed); every comparison leaves it out.
    invalid_reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.invalid_reason is None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, data: dict[str, Any]) -> "MarketDay":
        values = dict(data)
        values["candidates"] = [EventRow.load(row) for row in data["candidates"]]
        values["picks"] = [EventRow.load(row) for row in data["picks"]]
        values["tracked"] = [TrackedRow.load(row) for row in data.get("tracked", [])]
        values["reference_picks"] = [EventRow.load(row) for row in data.get("reference_picks", [])]
        # Runs written before days were marked invalid: judge them the same way.
        values["invalid_reason"] = data.get("invalid_reason") or invalid_reason_for(
            data["selection_mode"]
        )
        return cls(**values)

    # --- metrics ---------------------------------------------------------------------

    @property
    def five_stars(self) -> int:
        return sum(1 for row in self.candidates if row.stars == 5)

    def star_distribution(self) -> dict[str, int]:
        counts = Counter(row.stars for row in self.candidates)
        distribution = {str(stars): counts.get(stars, 0) for stars in STAR_LEVELS}
        distribution["unrated"] = counts.get(None, 0)
        return distribution

    def impact_gaps(self) -> list[EventRow]:
        """Rated candidates whose stars and triage impact differ by two or more."""
        return [
            row
            for row in self.candidates
            if row.stars is not None
            and row.impact is not None
            and abs(row.stars - row.impact) >= GAP_LEVELS
        ]

    def tw50_share(self) -> dict[str, Any]:
        """Company stories about non-Taiwan-50 companies among the edition's picks."""
        companies = [row for row in self.picks if row.company_story]
        outside = [row for row in companies if not row.tw50]
        return {
            "picks": len(self.picks),
            "company_stories": len(companies),
            "non_tw50_company_stories": len(outside),
            "share": len(outside) / len(self.picks) if self.picks else None,
        }

    def star_agreement(self) -> dict[str, int]:
        """Candidates production also rated, and how many got the same stars."""
        shared = [
            (row.stars, self.reference_stars[key])
            for row in self.candidates
            if (key := (row.prod_event_ids or [row.event_id])[0]) in self.reference_stars
        ]
        rated = [(mine, theirs) for mine, theirs in shared if mine is not None]
        return {
            "shared": len(shared),
            "same": sum(1 for mine, theirs in rated if mine == theirs),
            "higher": sum(1 for mine, theirs in rated if mine > theirs),
            "lower": sum(1 for mine, theirs in rated if mine < theirs),
        }

    def reference_overlap(self) -> dict[str, int]:
        """Picks that production also picked, and those that differ either way."""
        reference = {row.event_id for row in self.reference_picks}
        same = sum(1 for row in self.picks if reference & set(row.prod_event_ids))
        covered = {event_id for row in self.picks for event_id in row.prod_event_ids}
        return {
            "same": same,
            "replay_only": len(self.picks) - same,
            "production_only": sum(
                1 for row in self.reference_picks if row.event_id not in covered
            ),
        }


FALLBACK_REASON = "精選失敗, 退回粗分排序 (fallback)"


def invalid_reason_for(selection_mode: str) -> str | None:
    """Why a market day cannot be compared, judged from how it was selected."""
    return FALLBACK_REASON if selection_mode == "fallback" else None


# --- Markdown --------------------------------------------------------------------------


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _score(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _stars(value: int | None) -> str:
    return "未評" if value is None else f"★{value}"


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def _distribution(day: MarketDay) -> str:
    counts = day.star_distribution()
    text = "/".join(str(counts[str(stars)]) for stars in STAR_LEVELS)
    return text + (f" (+{counts['unrated']} 未評)" if counts["unrated"] else "")


def _overview(days: Sequence[MarketDay]) -> list[str]:
    lines = [
        "| 日期 | 市場 | 模式 | 五星 | 星等 5/4/3/2/1 | 版面 | 與正式相同 | 僅重播 | 僅正式"
        " | relevant 比例 | 市場達標文章 | 非台灣50個股稿 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for day in days:
        overlap = day.reference_overlap()
        share = day.tw50_share()
        tw50 = (
            f"{share['non_tw50_company_stories']}/{share['picks']} ({_percent(share['share'])})"
            if day.market == "tw_equity"
            else "—"
        )
        five = f"{day.five_stars}" + (" 需覆核" if day.five_star_review else "")
        mode = day.selection_mode + ("" if day.valid else " (作廢)")
        lines.append(
            f"| {day.edition_date} | {day.market} | {mode} | {five}"
            f" | {_distribution(day)} | {len(day.picks)} | {overlap['same']}"
            f" | {overlap['replay_only']} | {overlap['production_only']}"
            f" | {_percent(day.relevant / day.articles if day.articles else None)}"
            f" | {day.market_relevant} | {tw50} |"
        )
    return lines


def _event_table(rows: Sequence[EventRow], *, with_rank: bool) -> list[str]:
    head = "| 版面 | " if with_rank else "| "
    lines = [
        head + "名次 | 粗分 | 星等 | impact | 主題 | 標題 |",
        "| " + " | ".join(["---"] * (7 if with_rank else 6)) + " |",
    ]
    for row in rows:
        prefix = f"| {row.rank or '—'} | " if with_rank else "| "
        title = _cell(row.title) + (" (前版後續)" if row.follow_up else "")
        if row.tw50:
            title += f" [台灣50 {','.join(row.tw50)}]"
        lines.append(
            prefix + f"{row.position or '—'} | {_score(row.score)} | {_stars(row.stars)}"
            f" | {row.impact or '—'} | {row.topic or '—'} | {title} |"
        )
    return lines


def _agreement(days: Sequence[MarketDay]) -> list[str]:
    lines = [
        "## 與正式結果比較",
        "",
        "共同候選 = 本次前 40 名中也在正式前 40 名的事件; 星等比較只計本次有星等者。",
        "",
        "| 日期 | 市場 | 共同候選 | 星等相同 | 較高 | 較低 | 版面相同 | 僅重播 | 僅正式 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for day in days:
        stars, overlap = day.star_agreement(), day.reference_overlap()
        lines.append(
            f"| {day.edition_date} | {day.market} | {stars['shared']}/{len(day.candidates)}"
            f" | {stars['same']} | {stars['higher']} | {stars['lower']}"
            f" | {overlap['same']} | {overlap['replay_only']} | {overlap['production_only']} |"
        )
    return [*lines, ""]


def _gap_table(rows: Sequence[EventRow]) -> list[str]:
    lines = [
        "| 名次 | 粗分 | 星等 | impact | 入選 | 標題 | 精選 reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row.position or '—'} | {_score(row.score)} | {_stars(row.stars)}"
            f" | {row.impact or '—'} | {'是' if row.picked else '否'} | {_cell(row.title)}"
            f" | {_cell(row.reason) or '—'} |"
        )
    return lines


def _reproduction(days: Sequence[MarketDay]) -> list[str]:
    checked = [day for day in days if day.reproduction is not None]
    if not checked:
        return []
    lines = [
        "## 正式結果重現檢查",
        "",
        "| 日期 | 市場 | 候選名單 (id、名次、分數) | 星等 | 版面與排序 | 說明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for day in checked:
        result = day.reproduction or {}
        lines.append(
            f"| {day.edition_date} | {day.market}"
            f" | {'一致' if result.get('candidates') else '不一致'}"
            f" | {'一致' if result.get('stars') else '不一致'}"
            f" | {'一致' if result.get('picks') else '不一致'}"
            f" | {_cell('; '.join(result.get('details', [])) or '—')} |"
        )
    return [*lines, ""]


def _tracked(days: Sequence[MarketDay]) -> list[str]:
    lines = [
        "## 指定事件追蹤",
        "",
        "| 追蹤 | 日期 | 市場 | 名次 | 粗分 | 星等 | 入選 | impact | 比對到的事件標題 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    rows = 0
    for day in days:
        for tracked in day.tracked:
            event = tracked.event
            rows += 1
            lines.append(
                f"| {tracked.label} | {day.edition_date} | {day.market}"
                f" | {event.position or '—'} | {_score(event.score)} | {_stars(event.stars)}"
                f" | {'是' if event.picked else '否'} | {event.impact or '—'}"
                f" | {_cell(event.title)} |"
            )
    if not rows:
        lines.append("| — | — | — | — | — | — | — | — | 範圍內沒有比對到 |")
    return [*lines, ""]


def _cache_lines(stats: dict[str, Any]) -> list[str]:
    lines = [
        "| 呼叫 | 次數 | 命中 | 未命中 | 輸入 tokens | 輸出 tokens | 本次實際輸入 | 本次實際輸出 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    rows = {**stats.get("llm", {}), "embed (標題文字數)": stats.get("embed", {})}
    for label, row in rows.items():
        if not row or not row.get("calls"):
            continue
        lines.append(
            f"| {_cell(label)} | {row['calls']} | {row['hits']} | {row['misses']}"
            f" | {row['input_tokens']} | {row['output_tokens']}"
            f" | {row['live_input_tokens']} | {row['live_output_tokens']} |"
        )
    return lines


def invalid_lines(days: Sequence[MarketDay]) -> list[str]:
    """The market days left out of every comparison, and why."""
    invalid = [day for day in days if not day.valid]
    lines = ["## 作廢的市場日", ""]
    if not invalid:
        return [*lines, "- 無", ""]
    lines += [
        "以下市場日不列入任何比較 (五星數、版面差異、金標準、穩定入選集合):",
        "",
        "| 日期 | 市場 | 模式 | 原因 |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {day.edition_date} | {day.market} | {day.selection_mode} | {day.invalid_reason} |"
        for day in invalid
    ]
    return [*lines, ""]


def _day_notes(notes: dict[str, list[str]]) -> list[str]:
    if not notes:
        return []
    lines = ["## 每日處理紀錄", ""]
    for when, entries in sorted(notes.items()):
        lines += [f"- {when}: {entry}" for entry in entries]
    return [*lines, ""]


def summary(meta: dict[str, Any], days: Sequence[MarketDay]) -> str:
    """The run's ``summary.md``."""
    lines = [
        f"# 重點新聞重播 {meta['start']} 至 {meta['end']} ({meta['variant']}, {meta['stages']})",
        "",
        f"- 執行時間: {meta['started_at']}; 程式版本: `{meta.get('git_revision', '?')}`",
        f"- 資料快照: {meta['snapshot']} (正式庫 alembic `{meta.get('source_revision', '?')}`, "
        f"匯出於 {meta.get('exported_at', '?')})",
        f"- 模型: triage `{meta.get('triage_model', '?')}`、"
        f"editor `{meta.get('editor_model', '?')}`、"
        f"embedding `{meta.get('embedding_model', '?')}`",
        "",
        "## 方法與限制",
        "",
        *(f"- {note}" for note in meta.get("notes", [])),
        "",
        "## 快取與花費",
        "",
        *_cache_lines(meta.get("cache", {})),
        "",
        "## 每日每市場概覽",
        "",
        "「需覆核」表示五星超過軟上限。「與正式相同」以正式版面 (未移除、未隱藏的項目)比對。",
        "",
        *_overview(days),
        "",
        *invalid_lines(days),
        *_day_notes(meta.get("day_notes", {})),
        *_agreement(days),
        *_reproduction(days),
        *_tracked(days),
    ]
    for day in days:
        lines += [f"## {day.edition_date} {day.market}", ""]
        lines += [f"- {note}" for note in day.notes]
        if day.market == "tw_equity":
            share = day.tw50_share()
            lines.append(
                f"- 非台灣50個股稿: {share['non_tw50_company_stories']} 則"
                f" (個股稿 {share['company_stories']} 則, 版面 {share['picks']} 則)"
            )
        lines += ["", "### 版面", "", *_event_table(day.picks, with_rank=True), ""]
        lines += ["### 正式版面", "", *_event_table(day.reference_picks, with_rank=True), ""]
        lines += [
            f"### 粗分前 {TOP_SCORES} 名",
            "",
            *_event_table(day.candidates[:TOP_SCORES], with_rank=False),
            "",
        ]
        gaps = day.impact_gaps()
        lines += [f"### triage impact 與星等差距 ≥ {GAP_LEVELS} 級 ({len(gaps)} 則)", ""]
        lines += [*_gap_table(gaps), ""] if gaps else ["- 無", ""]
    return "\n".join(lines) + "\n"


def _picks_by_key(day: MarketDay) -> dict[str, EventRow]:
    """Picks keyed by the production event they stand for, so two triage replays compare."""
    return {(row.prod_event_ids or [row.event_id])[0]: row for row in day.picks}


def compare(
    left_meta: dict[str, Any],
    left: Sequence[MarketDay],
    right_meta: dict[str, Any],
    right: Sequence[MarketDay],
) -> str:
    """Side-by-side Markdown of two runs over the same dates and markets."""
    left_name = f"{left_meta['variant']}/{left_meta['stages']}"
    right_name = f"{right_meta['variant']}/{right_meta['stages']}"
    by_key = {(day.edition_date, day.market): day for day in right if day.valid}
    skipped = [day for day in [*left, *right] if not day.valid]
    lines = [
        f"# 重播比較: {left_name} vs {right_name}",
        "",
        f"- 左: `{left_meta['run_dir']}`",
        f"- 右: `{right_meta['run_dir']}`",
        "",
        *(
            [
                "作廢而不比較的市場日: "
                + "、".join(
                    f"{day.edition_date} {day.market} ({day.invalid_reason})" for day in skipped
                ),
                "",
            ]
            if skipped
            else []
        ),
        "## 五星數與星等分布",
        "",
        f"| 日期 | 市場 | 五星 {left_name} | 五星 {right_name}"
        f" | 星等 {left_name} | 星等 {right_name}"
        f" | 版面相同 | 僅左 | 僅右 | relevant {left_name} | relevant {right_name} |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    pairs = [(day, by_key.get((day.edition_date, day.market))) for day in left if day.valid]
    for day, other in pairs:
        if other is None:
            continue
        mine, theirs = _picks_by_key(day), _picks_by_key(other)
        lines.append(
            f"| {day.edition_date} | {day.market} | {day.five_stars} | {other.five_stars}"
            f" | {_distribution(day)} | {_distribution(other)}"
            f" | {len(mine.keys() & theirs.keys())} | {len(mine.keys() - theirs.keys())}"
            f" | {len(theirs.keys() - mine.keys())}"
            f" | {_percent(day.relevant / day.articles if day.articles else None)}"
            f" | {_percent(other.relevant / other.articles if other.articles else None)} |"
        )
    lines += ["", "## 版面差異", ""]
    for day, other in pairs:
        if other is None:
            continue
        mine, theirs = _picks_by_key(day), _picks_by_key(other)
        lines += [f"### {day.edition_date} {day.market}", ""]
        lines += [
            "| 版面 | 左 星等 | 左 粗分 | 右 星等 | 右 粗分 | 標題 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for key in [*mine, *(key for key in theirs if key not in mine)]:
            a, b = mine.get(key), theirs.get(key)
            side = "兩者" if a and b else ("僅左" if a else "僅右")
            row = a or b
            title = row.title if row else ""
            lines.append(
                f"| {side} | {_stars(a.stars) if a else '—'} | {_score(a.score) if a else '—'}"
                f" | {_stars(b.stars) if b else '—'} | {_score(b.score) if b else '—'}"
                f" | {_cell(title)} |"
            )
        lines.append("")
    runs: tuple[tuple[str, Sequence[MarketDay]], ...] = (
        (left_name, left),
        (right_name, [theirs for _, theirs in pairs if theirs is not None]),
    )
    for name, run_days in runs:
        gaps = [(gap_day, row) for gap_day in run_days for row in gap_day.impact_gaps()]
        lines += [
            f"## {name}: triage impact 與星等差距 ≥ {GAP_LEVELS} 級 ({len(gaps)} 則)",
            "",
            "| 日期 | 市場 | 名次 | 粗分 | 星等 | impact | 入選 | 標題 | 精選 reason |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {gap_day.edition_date} | {gap_day.market} | {row.position or '—'}"
            f" | {_score(row.score)} | {_stars(row.stars)} | {row.impact or '—'}"
            f" | {'是' if row.picked else '否'} | {_cell(row.title)} | {_cell(row.reason) or '—'} |"
            for gap_day, row in gaps
        ]
        lines.append("")
    lines += [
        "## 指定事件追蹤",
        "",
        f"| 追蹤 | 日期 | 市場 | 標題 | {left_name} 名次/粗分/星等/入選"
        f" | {right_name} 名次/粗分/星等/入選 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    def describe(row: EventRow | None) -> str:
        if row is None:
            return "—"
        return (
            f"{row.position or '—'} / {_score(row.score)} / {_stars(row.stars)}"
            f" / {'是' if row.picked else '否'}"
        )

    for day, other in pairs:
        if other is None:
            continue
        theirs_tracked = {
            (tracked.label, (tracked.event.prod_event_ids or [tracked.event.event_id])[0]): tracked
            for tracked in other.tracked
        }
        seen: set[tuple[str, str]] = set()
        for tracked in day.tracked:
            tracked_key = (
                tracked.label,
                (tracked.event.prod_event_ids or [tracked.event.event_id])[0],
            )
            seen.add(tracked_key)
            match = theirs_tracked.get(tracked_key)
            lines.append(
                f"| {tracked.label} | {day.edition_date} | {day.market}"
                f" | {_cell(tracked.event.title)}"
                f" | {describe(tracked.event)} | {describe(match.event if match else None)} |"
            )
        for tracked_key, tracked in theirs_tracked.items():
            if tracked_key not in seen:
                lines.append(
                    f"| {tracked.label} | {day.edition_date} | {day.market}"
                    f" | {_cell(tracked.event.title)} | — | {describe(tracked.event)} |"
                )
    return "\n".join(lines) + "\n"
