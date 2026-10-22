"""Replay past newsroom editions offline under a selection variant (read-only).

Production is only read: the date range is copied with read-only transactions
into a throwaway pgvector container on 127.0.0.1, and every replay writes to its
own copy there. No assembly, notifier or Slack call ever reaches production.

    prepare   copy a range into a template database (reused until --refresh)
    run       replay a range: --stages score | editor | triage, --variant baseline | p0
    compare   side-by-side Markdown of two run directories
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
from daily_insights_api.modules.newsroom.replay import metrics, runner, snapshot
from daily_insights_api.modules.newsroom.replay.cache import ReplayCacheMiss
from daily_insights_api.modules.newsroom.replay.pipeline import VariantUnsupportedError
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

    compare = commands.add_parser("compare", help="compare two run directories")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument("--output", type=Path, help="write here instead of stdout")

    commands.add_parser("teardown", help="remove the throwaway database container")
    options = parser.parse_args(args)
    if getattr(options, "start", None) and options.start > options.end:
        parser.error("--start must not be after --end")
    if options.command == "run" and not 1 <= options.concurrency <= runner.MAX_CONCURRENCY:
        parser.error(f"--concurrency must be between 1 and {runner.MAX_CONCURRENCY}")
    if options.command == "run":
        try:
            options.tracks = (*metrics.DEFAULT_TRACKS, *map(metrics.parse_track, options.track))
        except ValueError as error:
            parser.error(str(error))
    return options


def _environment(options: argparse.Namespace) -> snapshot.ReplayEnvironment:
    return snapshot.ReplayEnvironment(
        root=(options.root or _root()).expanduser().resolve(),
        source_container=options.source_container,
        keys_container=options.keys_container,
        server_port=options.port,
    )


def main(args: list[str] | None = None) -> int:
    options = parse_args(args)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    environment = _environment(options)
    try:
        if options.command == "teardown":
            snapshot.remove_server(environment)
            print(f"removed {environment.server_container}")
        elif options.command == "prepare":
            range_ = snapshot.Snapshot(options.start, options.end)
            prepared = snapshot.prepare(environment, range_, refresh=options.refresh)
            print(json.dumps(prepared, ensure_ascii=False, indent=2))
        elif options.command == "run":
            run_dir = asyncio.run(
                runner.run(
                    environment,
                    runner.RunOptions(
                        snapshot=snapshot.Snapshot(options.start, options.end),
                        variant=get_variant(options.variant),
                        stages=options.stages,
                        concurrency=options.concurrency,
                        offline=options.offline,
                        keep_database=options.keep_db,
                        tracks=options.tracks,
                    ),
                )
            )
            print(run_dir / "summary.md")
        else:
            left_meta, left = runner.read_run(options.left)
            right_meta, right = runner.read_run(options.right)
            text = metrics.compare(left_meta, left, right_meta, right)
            if options.output:
                options.output.write_text(text, encoding="utf-8")
                print(options.output)
            else:
                print(text, end="")
    except (snapshot.ReplayError, ReplayCacheMiss, VariantUnsupportedError) as error:
        print(f"newsroom replay failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
