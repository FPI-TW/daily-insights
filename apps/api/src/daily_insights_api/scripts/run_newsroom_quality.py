"""Print the newsroom quality report for a range of edition dates (read-only).

Run it before and after a pipeline change over equal-length ranges to compare
the same metrics (docs/architecture/daily-news.md, "品質指標"). It never calls
a model and never writes.
"""

import argparse
import asyncio
import sys
from datetime import date, timedelta

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.newsroom.clock import taipei_today
from daily_insights_api.modules.newsroom.quality import QualityReport, quality_report


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Newsroom quality report")
    parser.add_argument("--start", type=date.fromisoformat, help="first edition date")
    parser.add_argument("--end", type=date.fromisoformat, help="last edition date (inclusive)")
    parser.add_argument(
        "--sample", type=int, default=10, help="items to list for a fact check (0 for none)"
    )
    options = parser.parse_args(args)
    options.end = options.end or taipei_today()
    options.start = options.start or options.end - timedelta(days=6)
    if options.start > options.end:
        parser.error("--start must not be after --end")
    return options


def _percent(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.0%}" if denominator else "—"


def render(report: QualityReport) -> str:
    lines = [f"# 新聞品質指標 {report.start.isoformat()} 至 {report.end.isoformat()}", ""]
    lines += [
        "## 收稿與分群",
        "",
        "| 版次 | 文章 | 相關 | 早於收稿窗 2 小時以上 | 事件 | 多篇事件 | 上架事件 | 每百篇上架 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for day in report.collection:
        lines.append(
            f"| {day.edition_date.isoformat()} | {day.articles} | {day.relevant} "
            f"| {day.published_before_window} | {day.events} | {day.multi_article_events} "
            f"| {day.published_events} "
            f"| {day.published_events * 100 / day.articles if day.articles else 0:.1f} |"
        )
    model = sum(edition.model_items for edition in report.editions)
    removed = sum(edition.removed_model_items for edition in report.editions)
    manual = sum(edition.manual_items for edition in report.editions)
    edited = sum(edition.edited_items for edition in report.editions)
    lines += [
        "",
        "## 管理員編修",
        "",
        f"- 模型選入 {model} 則 管理員移除 {removed} 則 手動加入 {manual} 則"
        f" 換稿率 {_percent(removed + manual, model)}",
        f"- 可見項目 {report.visible_items} 則 改過標題 摘要或為何重要的 {edited} 則"
        f" 改字率 {_percent(edited, report.visible_items)}",
        "",
        "| 星等 | 模型選入 | 管理員移除 | 移除率 |",
        "| --- | --- | --- | --- |",
    ]
    for stars in (5, 4, 3, 2, 1):
        picked = sum(edition.stars.get(stars, 0) for edition in report.editions)
        dropped = sum(edition.removed_by_stars.get(stars, 0) for edition in report.editions)
        if picked:
            lines.append(f"| {stars} | {picked} | {dropped} | {_percent(dropped, picked)} |")
    lines += [
        "",
        "## 重複上架",
        "",
        f"- 同版重複 {len(report.same_edition_repeats)} 組"
        f" 跨日重複 {len(report.cross_day_repeats)} 則"
        f" 占可見項目 {_percent(len(report.cross_day_repeats), report.visible_items)}",
        "",
    ]
    for label, pairs in (
        ("同版", report.same_edition_repeats),
        ("跨日", report.cross_day_repeats),
    ):
        for pair in pairs:
            lines.append(
                f"- {label} {pair.edition_date.isoformat()} {pair.market} 「{pair.title}」"
                f" ↔ {pair.earlier_date.isoformat()} {pair.earlier_market}"
                f" 「{pair.earlier_title}」 {pair.similarity}"
            )
    lines += ["", "## 組稿的重複事件篩選", ""]
    if not report.dedupe_decisions:
        lines.append("- 範圍內沒有紀錄")
    for scope, outcomes in sorted(report.dedupe_decisions.items()):
        counts = " ".join(f"{outcome} {count}" for outcome, count in sorted(outcomes.items()))
        lines.append(f"- {scope} 提議 {sum(outcomes.values())} 組 {counts}")
    lines += ["", "## 主編評分", ""]
    if not report.editor_ratings:
        lines.append("- 範圍內沒有紀錄")
    else:
        lines += ["| 星等 | 候選 | 入選 |", "| --- | --- | --- |"]
        for stars in sorted(report.editor_ratings, reverse=True):
            rated, picked = report.editor_ratings[stars]
            lines.append(f"| {stars} | {rated} | {picked} |")
        lines.append("")
        lines.append(f"- 4 星以上未入選 {len(report.unpicked_high_stars)} 則")
        lines.extend(
            f"    - {item.edition_date.isoformat()} {item.market} 第 {item.position} 名"
            f" {item.stars} 星 {item.title}"
            for item in report.unpicked_high_stars
        )
    if report.fact_check_sample:
        lines += ["", "## 事實核對抽樣", ""]
        for item in report.fact_check_sample:
            lines.append(f"- {item['edition_date']} {item['market']} {item['headline']}")
            lines.append(f"    - 摘要 {item['summary']}")
            lines.extend(f"    - 來源 {source}" for source in item["sources"])
    return "\n".join(lines) + "\n"


async def build(options: argparse.Namespace) -> QualityReport:
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as database:
            return await quality_report(database, options.start, options.end, sample=options.sample)
    finally:
        await engine.dispose()


def main(args: list[str] | None = None) -> int:
    options = parse_args(args)
    try:
        report = asyncio.run(build(options))
    except Exception as error:
        print(f"newsroom quality report failed: {error}", file=sys.stderr)
        return 1
    print(render(report), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
