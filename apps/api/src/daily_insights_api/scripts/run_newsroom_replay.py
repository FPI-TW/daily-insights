"""Replay past newsroom editions offline under a selection variant (read-only).

Production is only read: the date range is copied with read-only transactions
into a throwaway pgvector container on 127.0.0.1, and every replay writes to its
own copy there. No assembly, notifier or Slack call ever reaches production.

    prepare   copy a range into a template database (reused until --refresh)
    run       replay a range: --stages score | editor | triage | retriage, --variant ...
              (--rep N draws the assembly again; retriage needs --base-variant)
    compare   side-by-side Markdown of two run directories
    report    several runs per variant: noise, stable picks, real differences, golden set
    golden    the golden set checked against runs
    cost      the round's spending from its ledger
    teardown  remove the throwaway database container

Run directories, the response cache and snapshots live under --root
(``NEWSROOM_REPLAY_ROOT``), outside the repository.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.modules.newsroom.assembly import PUBLISHED_LOOKBACK_DAYS
from daily_insights_api.modules.newsroom.replay import (
    costs,
    golden,
    metrics,
    multirun,
    retriage,
    runner,
    snapshot,
)
from daily_insights_api.modules.newsroom.replay.cache import ReplayCacheMiss
from daily_insights_api.modules.newsroom.variants import VARIANTS, get_variant

DEFAULT_START = date(2026, 10, 18)
DEFAULT_END = date(2026, 10, 22)
DEFAULT_ROOT = Path.home() / ".cache" / "daily-insights" / "newsroom-replay"


def _root() -> Path:
    configured = os.environ.get("NEWSROOM_REPLAY_ROOT")
    return Path(configured).expanduser().resolve() if configured else DEFAULT_ROOT


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline newsroom edition replay")
    parser.add_argument("--root", type=Path, default=None, help="replay data directory")
    parser.add_argument(
        "--source-container",
        default="daily-insights-newsroom-postgres-1",
        help="production Postgres container (read-only)",
    )
    parser.add_argument(
        "--keys-container",
        default="daily-insights-newsroom-api-1",
        help="container whose environment holds the provider keys",
    )
    parser.add_argument("--port", type=int, default=45433, help="throwaway database port")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_range(command: argparse.ArgumentParser) -> None:
        command.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
        command.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
        command.add_argument(
            "--lookback-days",
            type=int,
            default=None,
            help="published days before --start to copy (default: what the variant reads)",
        )

    prepare = commands.add_parser("prepare", help="copy a range out of production")
    add_range(prepare)
    prepare.add_argument("--refresh", action="store_true", help="export the range again")

    run = commands.add_parser("run", help="replay a prepared range")
    add_range(run)
    run.add_argument("--variant", choices=sorted(VARIANTS), default="baseline")
    run.add_argument("--stages", choices=runner.STAGES, default="score")
    run.add_argument(
        "--concurrency",
        type=int,
        default=6,
        help=f"parallel triage calls (1-{runner.MAX_CONCURRENCY})",
    )
    run.add_argument("--offline", action="store_true", help="fail on a cache miss")
    run.add_argument("--keep-db", action="store_true", help="keep the run's database copy")
    run.add_argument(
        "--track",
        action="append",
        default=[],
        metavar="LABEL=REGEX[@market,...]",
        help="follow another story by title (repeatable)",
    )
    run.add_argument(
        "--rep", type=int, default=1, help="repetition of the assembly (and re-triage) calls"
    )
    run.add_argument("--triage-rep", type=int, default=1, help="repetition of a full triage pass")
    run.add_argument(
        "--base-variant",
        choices=sorted(VARIANTS),
        help="retriage: the variant whose triage replay supplies the events",
    )
    run.add_argument(
        "--dry-run", action="store_true", help="retriage: select and estimate, call nothing"
    )
    run.add_argument(
        "--skip-day",
        action="append",
        default=[],
        metavar="YYYY-MM-DD=REASON",
        help="do not replay this day; mark it invalid with the reason (repeatable)",
    )
    defaults = retriage.Selection()
    run.add_argument("--select-min-impact", type=int, default=defaults.min_impact)
    run.add_argument(
        "--select-topic",
        action="append",
        default=None,
        help=f"retriage target topic (repeatable; default {','.join(defaults.topics)})",
    )
    run.add_argument("--select-min-escalation", type=int, default=defaults.min_escalation)
    run.add_argument("--select-title", default=defaults.title_pattern, metavar="REGEX")
    run.add_argument("--control-sample", type=int, default=defaults.control_sample)
    run.add_argument("--round", default=runner.DEFAULT_ROUND, help="ledger the spending goes to")

    compare = commands.add_parser("compare", help="compare two run directories")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument("--output", type=Path, help="write here instead of stdout")

    report = commands.add_parser("report", help="several runs per variant together")
    report.add_argument(
        "--group",
        action="append",
        required=True,
        metavar="NAME=RUN[,RUN...]",
        help="the repeated runs of one setting (repeatable)",
    )
    report.add_argument("--baseline", help="the group others are compared with")
    report.add_argument(
        "--against-production",
        action="append",
        default=[],
        metavar="NAME",
        help="also compare each run of this group with production's editions",
    )
    report.add_argument("--golden", type=Path, default=golden.GOLDEN_SET_PATH)
    report.add_argument("--title", default="多次重播報表")
    report.add_argument("--output", type=Path, help="write here instead of stdout")

    golden_ = commands.add_parser("golden", help="check the golden set against runs")
    golden_.add_argument("runs", type=Path, nargs="+")
    golden_.add_argument("--golden", type=Path, default=golden.GOLDEN_SET_PATH)

    cost = commands.add_parser("cost", help="the round's spending")
    cost.add_argument("--round", default=runner.DEFAULT_ROUND)

    commands.add_parser("teardown", help="remove the throwaway database container")
    options = parser.parse_args(args)
    if getattr(options, "start", None) and options.start > options.end:
        parser.error("--start must not be after --end")
    if getattr(options, "lookback_days", None) is not None and options.lookback_days < 1:
        parser.error("--lookback-days must be at least 1")
    if options.command == "run" and not 1 <= options.concurrency <= runner.MAX_CONCURRENCY:
        parser.error(f"--concurrency must be between 1 and {runner.MAX_CONCURRENCY}")
    if options.command == "run":
        try:
            options.tracks = (*metrics.DEFAULT_TRACKS, *map(metrics.parse_track, options.track))
            options.skip_days = tuple(_skip_day(text) for text in options.skip_day)
        except ValueError as error:
            parser.error(str(error))
        if options.rep < 1 or options.triage_rep < 1:
            parser.error("--rep and --triage-rep start at 1")
        if options.stages == "retriage" and options.base_variant is None:
            parser.error("--stages retriage needs --base-variant")
        if options.dry_run and options.stages != "retriage":
            parser.error("--dry-run only applies to --stages retriage")
    return options


def _snapshot(options: argparse.Namespace) -> snapshot.Snapshot:
    """The range, copied with as many earlier published days as the variants read."""
    lookback = options.lookback_days
    if lookback is None:
        names = [getattr(options, "variant", None), getattr(options, "base_variant", None)]
        lookback = max(
            [PUBLISHED_LOOKBACK_DAYS]
            + [get_variant(name).repeat_lookback_days for name in names if name]
        )
    return snapshot.Snapshot(options.start, options.end, lookback_days=lookback)


def _skip_day(text: str) -> tuple[date, str]:
    day, separator, reason = text.partition("=")
    if not separator or not reason.strip():
        raise ValueError("--skip-day is YYYY-MM-DD=REASON")
    return date.fromisoformat(day.strip()), reason.strip()


def _runs_dir(options: argparse.Namespace) -> Path:
    return _environment(options).runs_dir


def _run_path(options: argparse.Namespace, name: str) -> Path:
    path = Path(name).expanduser()
    return path if path.is_absolute() or path.exists() else _runs_dir(options) / name


def _groups(options: argparse.Namespace) -> list[multirun.Group]:
    groups: list[multirun.Group] = []
    for text in options.group:
        name, separator, runs = text.partition("=")
        if not separator:
            raise snapshot.ReplayError("--group is NAME=RUN[,RUN...]")
        members = []
        for run_name in (part.strip() for part in runs.split(",") if part.strip()):
            path = _run_path(options, run_name)
            meta, days = runner.read_run(path)
            members.append(multirun.Run(path.name, meta, days))
        groups.append(multirun.Group(name.strip(), members))
    return groups


def _selection(options: argparse.Namespace) -> retriage.Selection:
    defaults = retriage.Selection()
    return retriage.Selection(
        min_impact=options.select_min_impact,
        topics=tuple(options.select_topic) if options.select_topic else defaults.topics,
        min_escalation=options.select_min_escalation,
        title_pattern=options.select_title,
        control_sample=options.control_sample,
    )


def _environment(options: argparse.Namespace) -> snapshot.ReplayEnvironment:
    return snapshot.ReplayEnvironment(
        root=(options.root or _root()).expanduser().resolve(),
        source_container=options.source_container,
        keys_container=options.keys_container,
        server_port=options.port,
    )


def _emit(text: str, output: Path | None) -> None:
    if output:
        output.write_text(text, encoding="utf-8")
        print(output)
    else:
        print(text, end="")


def main(args: list[str] | None = None) -> int:
    options = parse_args(args)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    environment = _environment(options)
    try:
        if options.command == "teardown":
            snapshot.remove_server(environment)
            print(f"removed {environment.server_container}")
        elif options.command == "prepare":
            range_ = _snapshot(options)
            prepared = snapshot.prepare(environment, range_, refresh=options.refresh)
            print(json.dumps(prepared, ensure_ascii=False, indent=2))
        elif options.command == "run":
            run_dir = asyncio.run(
                runner.run(
                    environment,
                    runner.RunOptions(
                        snapshot=_snapshot(options),
                        variant=get_variant(options.variant),
                        stages=options.stages,
                        concurrency=options.concurrency,
                        offline=options.offline,
                        keep_database=options.keep_db,
                        tracks=options.tracks,
                        rep=options.rep,
                        triage_rep=options.triage_rep,
                        base_variant=(
                            get_variant(options.base_variant) if options.base_variant else None
                        ),
                        selection=_selection(options),
                        dry_run=options.dry_run,
                        skip_days=options.skip_days,
                        round=options.round,
                    ),
                )
            )
            if options.dry_run:
                meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
                print(json.dumps(meta["retriage_estimate"], ensure_ascii=False, indent=2))
            print(run_dir / ("run.json" if options.dry_run else "summary.md"))
        elif options.command == "report":
            text = multirun.report(
                _groups(options),
                baseline=options.baseline,
                items=golden.load(options.golden),
                title=options.title,
                against_production=options.against_production,
            )
            _emit(text, options.output)
        elif options.command == "golden":
            items = golden.load(options.golden)
            lines = [*golden.items_markdown(items), ""]
            for path in options.runs:
                _, days = runner.read_run(_run_path(options, str(path)))
                lines += [f"## {path.name}", ""]
                lines += [*golden.verdicts_markdown(golden.evaluate_all(items, days)), ""]
            print("\n".join(lines))
        elif options.command == "cost":
            entries = costs.read_ledger(_environment(options).ledger_path(options.round))
            print(costs.ledger_markdown(entries), end="")
        else:
            left_meta, left = runner.read_run(options.left)
            right_meta, right = runner.read_run(options.right)
            _emit(metrics.compare(left_meta, left, right_meta, right), options.output)
    except (snapshot.ReplayError, ReplayCacheMiss) as error:
        print(f"newsroom replay failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
