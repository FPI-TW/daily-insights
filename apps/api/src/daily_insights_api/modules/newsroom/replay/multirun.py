"""Reading several runs of each variant together: noise, stable picks and real differences.

LLM answers differ between identical runs, so one run per variant cannot tell
a variant's effect from noise. A group is the repeated runs of one setting
(``--rep 1..3``). For each group this module reports, per market day:

- the stable picks: events picked in a majority of the group's valid runs;
- the five-star count's minimum, median and maximum;
- how many slots differ between two of its runs, and at which star boundary
  each difference sits (5/4, 4/3, 3-star fill ...).

Against the baseline group, a difference is *real* only when it falls outside
the baseline's own spread: a five-star median outside the baseline's range, a
slot change larger than any two baseline runs show, or an event stably picked
on one side and never on the other.

Runs of one group share their events (same triage replay), so events are told
apart by id within a group; across groups they are matched through the
production event most of their articles came from.
"""

import itertools
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from daily_insights_api.modules.newsroom.replay import golden
from daily_insights_api.modules.newsroom.replay.metrics import EventRow, MarketDay

Key = Callable[[EventRow], str]


def event_key(row: EventRow) -> str:
    return row.event_id


def production_key(row: EventRow) -> str:
    return (row.prod_event_ids or [row.event_id])[0]


@dataclass(frozen=True, slots=True)
class Run:
    name: str
    meta: dict[str, Any]
    days: list[MarketDay]

    def day(self, when: str, market: str) -> MarketDay | None:
        for day in self.days:
            if day.edition_date == when and day.market == market:
                return day
        return None


@dataclass(frozen=True, slots=True)
class Group:
    name: str
    runs: list[Run]

    def slots(self) -> list[tuple[str, str]]:
        """Every (date, market) any run of the group covers, in order."""
        seen = dict.fromkeys(
            (day.edition_date, day.market) for run in self.runs for day in run.days
        )
        return list(seen)

    def valid_days(self, when: str, market: str) -> list[tuple[Run, MarketDay]]:
        found = [(run, run.day(when, market)) for run in self.runs]
        return [(run, day) for run, day in found if day is not None and day.valid]


# --- Slot differences ------------------------------------------------------------------


def boundary(mine: int | None, theirs: int | None) -> str:
    """Where an event picked on one side only sits between the two ratings."""
    if theirs is None:
        return "對方未評 (不在前 40、前版排除或分群不同)"
    if mine is None:
        return "本方未評"
    if mine == theirs:
        return {3: "3 星補位", 4: "4 星名額"}.get(mine, f"同為 {mine} 星")
    high, low = max(mine, theirs), min(mine, theirs)
    return f"{high}/{low} 星邊界"


@dataclass(frozen=True, slots=True)
class SlotDiff:
    only_left: int
    only_right: int
    boundaries: Counter[str]
    star_flips: Counter[str]

    @property
    def changed(self) -> int:
        return max(self.only_left, self.only_right)


def _stars(day: MarketDay, key: Key) -> dict[str, int | None]:
    stars: dict[str, int | None] = {}
    for row in [*day.candidates, *day.picks]:
        stars.setdefault(key(row), row.stars)
    return stars


def slot_diff(left: MarketDay, right: MarketDay, key: Key) -> SlotDiff:
    """Picks on one side only, classified by the star boundary they sit on."""
    left_picks = {key(row): row for row in left.picks}
    right_picks = {key(row): row for row in right.picks}
    left_stars, right_stars = _stars(left, key), _stars(right, key)
    boundaries: Counter[str] = Counter()
    for event, row in left_picks.items():
        if event not in right_picks:
            boundaries[boundary(row.stars, right_stars.get(event))] += 1
    for event, row in right_picks.items():
        if event not in left_picks:
            boundaries[boundary(row.stars, left_stars.get(event))] += 1
    flips: Counter[str] = Counter()
    for event, stars in left_stars.items():
        other = right_stars.get(event)
        if stars is not None and other is not None and stars != other:
            flips[f"{max(stars, other)}/{min(stars, other)}"] += 1
    return SlotDiff(
        only_left=len(left_picks.keys() - right_picks.keys()),
        only_right=len(right_picks.keys() - left_picks.keys()),
        boundaries=boundaries,
        star_flips=flips,
    )


def reference_day(day: MarketDay) -> MarketDay:
    """Production's edition of a replayed market day, as a market day of its own."""
    return MarketDay(
        edition_date=day.edition_date,
        market=day.market,
        selection_mode="production",
        candidates=[
            EventRow(event_id=event, title="", position=None, score=None, stars=stars)
            for event, stars in day.reference_stars.items()
        ],
        picks=day.reference_picks,
    )


@dataclass(frozen=True, slots=True)
class NoiseSummary:
    label: str
    pairs: int
    slots: int
    changed: int
    per_pair: list[int]
    boundaries: Counter[str]
    star_flips: Counter[str]


def pairwise_noise(
    label: str,
    pairs: Sequence[tuple[Run, Run]],
    *,
    key: Key = event_key,
    right_is_reference: bool = False,
) -> NoiseSummary:
    """Slot changes summed over market days, per pair of runs."""
    per_pair: list[int] = []
    boundaries: Counter[str] = Counter()
    flips: Counter[str] = Counter()
    slots = 0
    for left, right in pairs:
        changed = 0
        for day in left.days:
            other = day if right_is_reference else right.day(day.edition_date, day.market)
            if other is None or not day.valid or not other.valid:
                continue
            if right_is_reference:
                other = reference_day(day)
            diff = slot_diff(day, other, key)
            changed += diff.changed
            slots += max(len(day.picks), len(other.picks))
            boundaries.update(diff.boundaries)
            flips.update(diff.star_flips)
        per_pair.append(changed)
    return NoiseSummary(label, len(pairs), slots, sum(per_pair), per_pair, boundaries, flips)


def within_pairs(group: Group) -> list[tuple[Run, Run]]:
    return list(itertools.combinations(group.runs, 2))


# --- Per market day statistics ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FiveStars:
    counts: list[int]

    @property
    def low(self) -> int:
        return min(self.counts)

    @property
    def high(self) -> int:
        return max(self.counts)

    @property
    def median(self) -> float:
        return statistics.median(self.counts)

    def text(self) -> str:
        if not self.counts:
            return "—"
        median = f"{self.median:g}"
        return f"{self.low}/{median}/{self.high}"


def five_stars(group: Group, when: str, market: str) -> FiveStars:
    return FiveStars([day.five_stars for _, day in group.valid_days(when, market)])


def majority(runs: int) -> int:
    return runs // 2 + 1


def pick_counts(group: Group, when: str, market: str, key: Key) -> tuple[Counter[str], int]:
    days = group.valid_days(when, market)
    counts: Counter[str] = Counter()
    for _, day in days:
        counts.update({key(row) for row in day.picks})
    return counts, len(days)


def stable_picks(group: Group, when: str, market: str, key: Key) -> list[tuple[EventRow, int]]:
    """Events picked in a majority of the group's valid runs, with how often."""
    counts, runs = pick_counts(group, when, market, key)
    if not runs:
        return []
    rows: dict[str, EventRow] = {}
    for _, day in group.valid_days(when, market):
        for row in day.picks:
            rows.setdefault(key(row), row)
    return sorted(
        ((rows[event], count) for event, count in counts.items() if count >= majority(runs)),
        key=lambda pair: (-pair[1], -(pair[0].stars or 0), pair[0].rank or 99),
    )


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


# --- Report ----------------------------------------------------------------------------


def _counter_text(counter: Counter[str]) -> str:
    return "、".join(f"{label} {count}" for label, count in counter.most_common()) or "—"


def noise_lines(summaries: Sequence[NoiseSummary]) -> list[str]:
    lines = [
        "| 比較 | 配對數 | 每對版位變動 | 平均 | 版位數 (每對) | 翻動所在邊界 (合計)"
        " | 共同候選星等變動 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for summary in summaries:
        average = summary.changed / summary.pairs if summary.pairs else 0
        per_slots = summary.slots // summary.pairs if summary.pairs else 0
        lines.append(
            f"| {summary.label} | {summary.pairs} | {', '.join(map(str, summary.per_pair))}"
            f" | {average:.1f} | {per_slots} | {_counter_text(summary.boundaries)}"
            f" | {_counter_text(summary.star_flips)} |"
        )
    return lines


def _invalid(groups: Sequence[Group]) -> list[str]:
    lines = ["## 作廢的市場日", ""]
    rows = [
        f"| {group.name} | {run.name} | {day.edition_date} | {day.market} | {day.invalid_reason} |"
        for group in groups
        for run in group.runs
        for day in run.days
        if not day.valid
    ]
    if not rows:
        return [*lines, "- 無", ""]
    return [
        *lines,
        "| 組 | run | 日期 | 市場 | 原因 |",
        "| --- | --- | --- | --- | --- |",
        *rows,
        "",
    ]


def _real_five(variant: FiveStars, baseline: FiveStars) -> bool:
    return bool(variant.counts and baseline.counts) and not (
        baseline.low <= variant.median <= baseline.high
    )


def report(
    groups: Sequence[Group],
    *,
    baseline: str | None,
    items: Sequence[golden.GoldenItem] = (),
    title: str = "多次重播報表",
    against_production: Sequence[str] = (),
) -> str:
    """Markdown over every group: five stars, stable picks, noise, real differences, golden set."""
    base = next((group for group in groups if group.name == baseline), None)
    slots = list(dict.fromkeys(slot for group in groups for slot in group.slots()))
    lines = [
        f"# {title}",
        "",
        "| 組 | run |",
        "| --- | --- |",
        *(f"| {group.name} | {', '.join(run.name for run in group.runs)} |" for group in groups),
        "",
        "- 五星數格式: 最小/中位數/最大 (只計有效的市場日)。",
        "- 穩定入選: 在該組有效 run 中過半數 (3 次中至少 2 次) 入選。",
        "- 實際差異: 五星中位數落在 baseline 自己的最小~最大範圍之外; 版位變動大於 baseline"
        " 任兩次之間的最大變動; 或事件在一方穩定入選、另一方一次都沒入選。",
        "",
        *_invalid(groups),
        "## 噪音拆解",
        "",
        "每對 run 在所有有效市場日的版位變動合計 (一方有、另一方沒有的入選事件數, 取兩方向較大者)。"
        "翻動所在邊界: 只在一方入選的事件, 以兩方給它的星等歸類; 「3 星補位」= 兩方都給 ★3,"
        " 只是補位名額讓給了別則; 「對方未評」= 對方沒把它放進前 40 名 (前版排除、去重或分群不同)。"
        " 共同候選星等變動 = 兩方前 40 名都有的事件中星等不同者。",
        "",
        *noise_lines(
            [
                *(
                    pairwise_noise(f"{group.name} 組內 (兩兩)", within_pairs(group))
                    for group in groups
                    if len(group.runs) > 1
                ),
                *(
                    pairwise_noise(
                        f"{group.name} 各 run vs 正式版面",
                        [(run, run) for run in group.runs],
                        key=production_key,
                        right_is_reference=True,
                    )
                    for group in groups
                    if group.name in against_production
                ),
            ]
        ),
        "",
        "## 每市場每日五星數",
        "",
        "| 日期 | 市場 | " + " | ".join(group.name for group in groups) + " |",
        "| --- | --- | " + " | ".join("---" for _ in groups) + " |",
    ]
    for when, market in slots:
        base_five = five_stars(base, when, market) if base else None
        cells = []
        for group in groups:
            stats = five_stars(group, when, market)
            flag = (
                " **實際差異**"
                if base_five is not None and group is not base and _real_five(stats, base_five)
                else ""
            )
            cells.append(stats.text() + flag)
        lines.append(f"| {when} | {market} | " + " | ".join(cells) + " |")
    lines += ["", "## 版位變動 (同組 run 之間 = 噪音; 與 baseline 之間 = 比較)", ""]
    lines += [
        "| 日期 | 市場 | "
        + " | ".join(f"{group.name} 組內" for group in groups)
        + (
            " | "
            + " | ".join(f"{group.name} vs {base.name}" for group in groups if group is not base)
            if base
            else ""
        )
        + " |",
        "| --- | --- | "
        + " | ".join("---" for _ in groups)
        + ("".join(" | ---" for group in groups if group is not base) if base else "")
        + " |",
    ]
    for when, market in slots:
        cells = []
        within: dict[str, list[int]] = {}
        for group in groups:
            changes = [
                slot_diff(left_day, right_day, event_key).changed
                for (_, left_day), (_, right_day) in itertools.combinations(
                    group.valid_days(when, market), 2
                )
            ]
            within[group.name] = changes
            cells.append(f"{min(changes)}~{max(changes)}" if changes else "—")
        if base:
            base_max = max(within.get(base.name) or [0])
            for group in groups:
                if group is base:
                    continue
                cross = [
                    slot_diff(left_day, right_day, production_key).changed
                    for _, left_day in group.valid_days(when, market)
                    for _, right_day in base.valid_days(when, market)
                ]
                text = f"{min(cross)}~{max(cross)}" if cross else "—"
                if cross and min(cross) > base_max:
                    text += " **實際差異**"
                cells.append(text)
        lines.append(f"| {when} | {market} | " + " | ".join(cells) + " |")
    lines += ["", "## 穩定入選集合", ""]
    for when, market in slots:
        lines += [f"### {when} {market}", ""]
        base_counts = pick_counts(base, when, market, production_key) if base else None
        for group in groups:
            stable = stable_picks(group, when, market, event_key)
            runs = len(group.valid_days(when, market))
            lines.append(f"- **{group.name}** ({runs} 次有效):")
            if not stable:
                lines.append("  - (無)")
            for row, count in stable:
                flag = ""
                if base_counts is not None and group is not base and base_counts[1]:
                    if base_counts[0][production_key(row)] == 0:
                        flag = " **實際差異: baseline 一次都沒入選**"
                lines.append(
                    f"  - {count}/{runs} ★{row.stars if row.stars is not None else '—'}"
                    f" {_cell(row.title)}{flag}"
                )
            if base is not None and group is not base and base_counts is not None:
                mine, mine_runs = pick_counts(group, when, market, production_key)
                if mine_runs:
                    for row, count in stable_picks(base, when, market, production_key):
                        if mine[production_key(row)] == 0:
                            lines.append(
                                f"  - **實際差異: baseline 穩定入選 ({count} 次)"
                                f" 但本組一次都沒入選** {_cell(row.title)}"
                            )
        lines.append("")
    if items:
        lines += ["## 金標準逐項結果", ""]
        lines += [*golden.items_markdown(items), ""]
        for group in groups:
            lines += [f"### {group.name}", ""]
            summary_rows = []
            for item in items:
                verdicts = [golden.evaluate(item, run.days) for run in group.runs]
                judged = [verdict for verdict in verdicts if verdict.passed is not None]
                passed = sum(1 for verdict in judged if verdict.passed)
                stars = ", ".join(
                    "—" if verdict.stars is None else f"★{verdict.stars}" for verdict in verdicts
                )
                status = (
                    "無法判定"
                    if not judged
                    else ("通過" if passed >= majority(len(judged)) else "失敗")
                )
                summary_rows.append(
                    f"| {item.id} | `{item.expect}` | {status} | {passed}/{len(judged)}"
                    f" | {stars} | {_cell(item.description)} |"
                )
            lines += [
                "| id | 預期 | 多數決 | 通過次數 | 各 run 實際星等 | 說明 |",
                "| --- | --- | --- | --- | --- | --- |",
                *summary_rows,
                "",
            ]
            for run in group.runs:
                lines += [
                    f"#### {run.name}",
                    "",
                    *golden.verdicts_markdown(golden.evaluate_all(items, run.days)),
                    "",
                ]
    return "\n".join(lines) + "\n"
