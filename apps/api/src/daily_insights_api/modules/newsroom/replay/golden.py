"""The golden set: stories whose stars a replay must get right, checked per run.

Each item names a story by title keywords, a date range and a market, and an
expected star condition (``eq 5``, ``gte 4``, ``lt 5``). Every keyword (a
regular expression) must appear in one and the same title, the event's working
title or one of its reports' titles, and none of the ``exclude`` expressions
may; an event matches when any of its titles does. ``exclude_titles`` drops an
event whose working title matches, whatever its reports say (a mixed report).

One story can be several events (split by triage, or continued over days). A
"must reach" condition (``eq``, ``gte``, ``gt``) passes when the best-rated
match meets it; a "must stay below" condition (``lt``, ``lte``) fails as soon as
any match breaks it. An event outside the editor's top 40 has no stars: it
counts as missing, which fails a "must reach" item and passes a "must stay
below" one. Invalid market days are not judged.
"""

import json
import operator
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from daily_insights_api.modules.newsroom.replay.metrics import EventRow, MarketDay

GOLDEN_SET_PATH = Path(__file__).with_name("golden_set.json")
OPERATORS: dict[str, Callable[[int, int], bool]] = {
    "eq": operator.eq,
    "gte": operator.ge,
    "gt": operator.gt,
    "lte": operator.le,
    "lt": operator.lt,
}
REACHING = ("eq", "gte", "gt")


@dataclass(frozen=True, slots=True)
class GoldenItem:
    id: str
    description: str
    market: str
    start: date
    end: date
    keywords: tuple[str, ...]
    exclude: tuple[str, ...]
    expect: str
    annotator: str
    status: str
    note: str = ""
    exclude_titles: tuple[str, ...] = ()

    @property
    def operator(self) -> str:
        return self.expect.split()[0]

    @property
    def stars(self) -> int:
        return int(self.expect.split()[1])

    def holds(self, stars: int) -> bool:
        return OPERATORS[self.operator](stars, self.stars)

    def matches_text(self, text: str) -> bool:
        return all(re.search(word, text, re.IGNORECASE) for word in self.keywords) and not any(
            re.search(word, text, re.IGNORECASE) for word in self.exclude
        )

    def matches(self, row: EventRow) -> bool:
        if any(re.search(word, row.title, re.IGNORECASE) for word in self.exclude_titles):
            return False
        return any(self.matches_text(text) for text in row.texts() if text)


def _item(data: dict[str, Any]) -> GoldenItem:
    expect = str(data["expect"])
    parts = expect.split()
    if len(parts) != 2 or parts[0] not in OPERATORS or not parts[1].isdigit():
        raise ValueError(f"golden item {data.get('id')}: bad expect {expect!r}")
    item = GoldenItem(
        id=str(data["id"]),
        description=str(data["description"]),
        market=str(data["market"]),
        start=date.fromisoformat(data["dates"]["start"]),
        end=date.fromisoformat(data["dates"]["end"]),
        keywords=tuple(data["keywords"]),
        exclude=tuple(data.get("exclude", ())),
        expect=expect,
        annotator=str(data["annotator"]),
        status=str(data["status"]),
        note=str(data.get("note", "")),
        exclude_titles=tuple(data.get("exclude_titles", ())),
    )
    for word in (*item.keywords, *item.exclude, *item.exclude_titles):
        re.compile(word)
    return item


def load(path: Path = GOLDEN_SET_PATH) -> list[GoldenItem]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [_item(row) for row in data["items"]]


@dataclass(frozen=True, slots=True)
class Match:
    edition_date: str
    row: EventRow


@dataclass(frozen=True, slots=True)
class Verdict:
    item: GoldenItem
    # None when every market day of the item was invalid (or outside the run).
    passed: bool | None
    # The stars the verdict rests on (best match, or worst for "stay below").
    stars: int | None
    matches: tuple[Match, ...]
    detail: str

    @property
    def label(self) -> str:
        if self.passed is None:
            return "無法判定"
        return "通過" if self.passed else "失敗"


def _in_range(item: GoldenItem, day: MarketDay) -> bool:
    return (
        day.market == item.market and item.start <= date.fromisoformat(day.edition_date) <= item.end
    )


def evaluate(item: GoldenItem, days: Sequence[MarketDay]) -> Verdict:
    window = [day for day in days if _in_range(item, day)]
    valid = [day for day in window if day.valid]
    if not valid:
        reason = "範圍內的市場日全部作廢" if window else "本次重播不含此日期或市場"
        return Verdict(item, None, None, (), reason)
    matches: list[Match] = []
    for day in valid:
        seen: set[str] = set()
        for row in [*day.candidates, *day.picks]:
            if row.event_id not in seen and item.matches(row):
                seen.add(row.event_id)
                matches.append(Match(day.edition_date, row))
    rated = [match.row.stars for match in matches if match.row.stars is not None]
    skipped = len(window) - len(valid)
    suffix = f" (另有 {skipped} 個市場日作廢未判)" if skipped else ""
    if item.operator in REACHING:
        best = max(rated, default=None)
        if best is None:
            detail = "對到的事件都未評" if matches else "沒有對到有星等的事件 (未進前 40 或未評)"
            return Verdict(item, False, None, tuple(matches), detail + suffix)
        return Verdict(item, item.holds(best), best, tuple(matches), f"最高 ★{best}" + suffix)
    worst = max(rated, default=None)
    if worst is None:
        return Verdict(item, True, None, tuple(matches), "沒有對到有星等的事件" + suffix)
    return Verdict(item, item.holds(worst), worst, tuple(matches), f"最高 ★{worst}" + suffix)


def evaluate_all(items: Sequence[GoldenItem], days: Sequence[MarketDay]) -> list[Verdict]:
    return [evaluate(item, days) for item in items]


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def _stars(value: int | None) -> str:
    return "—" if value is None else f"★{value}"


def items_markdown(items: Sequence[GoldenItem]) -> list[str]:
    lines = [
        "| id | 說明 | 市場 | 日期 | 標題關鍵字 (同一標題全部出現) | 排除 | 預期 | 標註者 | 狀態"
        " | 備註 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in items:
        excluded = " / ".join(
            [
                *(f"`{word}`" for word in item.exclude),
                *(f"事件標題 `{word}`" for word in item.exclude_titles),
            ]
        )
        lines.append(
            f"| {item.id} | {_cell(item.description)} | {item.market}"
            f" | {item.start:%m/%d}~{item.end:%m/%d}"
            f" | {_cell(' + '.join(f'`{word}`' for word in item.keywords))}"
            f" | {_cell(excluded) or '—'}"
            f" | `{item.expect}` | {_cell(item.annotator)} | {item.status}"
            f" | {_cell(item.note) or '—'} |"
        )
    return lines


def verdicts_markdown(verdicts: Sequence[Verdict]) -> list[str]:
    lines = [
        "| id | 預期 | 結果 | 實際星等 | 對到的事件 (日期 名次 星等 標題) | 說明 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for verdict in verdicts:
        events = "; ".join(
            f"{match.edition_date[5:]} #{match.row.position or '—'}"
            f" {_stars(match.row.stars)}{'入選' if match.row.picked else ''}"
            f" {match.row.title}"
            for match in verdict.matches
        )
        lines.append(
            f"| {verdict.item.id} | `{verdict.item.expect}` | {verdict.label}"
            f" | {_stars(verdict.stars)} | {_cell(events) or '—'} | {_cell(verdict.detail)} |"
        )
    return lines
