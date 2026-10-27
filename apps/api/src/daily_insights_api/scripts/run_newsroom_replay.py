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

    agreement-sample  draw articles production triaged and rebuild their exact requests
    agreement-ask     ask those requests again of the --llm-* provider (one repetition)
    agreement-report  compare the answers with production and with each other

The --llm-* options send triage and editor calls to another OpenAI-compatible
provider (e.g. OpenCode Go) instead of the one the production containers name;
its key is read from --llm-key-file only when a call goes out.

Run directories, the response cache and snapshots live under --root
(``NEWSROOM_REPLAY_ROOT``), outside the repository.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.modules.newsroom import triage
from daily_insights_api.modules.newsroom.assembly import PUBLISHED_LOOKBACK_DAYS
from daily_insights_api.modules.newsroom.replay import (
    agreement,
    costs,
    golden,
    metrics,
    multirun,
    retriage,
    runner,
    snapshot,
)
from daily_insights_api.modules.newsroom.replay.cache import ReplayCacheMiss, ResponseCache
from daily_insights_api.modules.newsroom.replay.provider import (
    EVAL_USER_AGENT,
    LlmProvider,
    parse_extra_body,
)
from daily_insights_api.modules.newsroom.variants import BASELINE, VARIANTS, get_variant

DEFAULT_START = date(2026, 10, 18)
DEFAULT_END = date(2026, 10, 22)
DEFAULT_SERVER_CONTAINER = "newsroom-replay-pg"
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
    parser.add_argument(
        "--server-container",
        default=DEFAULT_SERVER_CONTAINER,
        help="name of the throwaway pgvector container",
    )
    parser.add_argument("--llm-base-url", help="another provider's OpenAI-compatible base URL")
    parser.add_argument("--llm-model", help="that provider's model (triage and editor)")
    parser.add_argument("--llm-key-file", type=Path, help="file holding that provider's key")
    parser.add_argument(
        "--llm-extra-body",
        default="{}",
        metavar="JSON",
        help='extra request fields, e.g. \'{"thinking":{"type":"disabled"}}\'',
    )
    parser.add_argument("--llm-user-agent", default=EVAL_USER_AGENT)
    parser.add_argument(
        "--llm-session-header", default=None, help="header for the stable session id"
    )
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
        "--via",
        choices=sorted(VARIANTS),
        help="retriage: re-judge energy articles under this variant first (e.g. p0b)",
    )
    run.add_argument("--via-rep", type=int, default=1, help="repetition of the --via step")
    run.add_argument(
        "--select",
        choices=retriage.SELECT_MODES,
        default="energy",
        help="retriage: what the run variant re-judges (energy, pool = top-40 events, none)",
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

    sample = commands.add_parser("agreement-sample", help="draw and rebuild triage requests")
    sample.add_argument("--name", required=True, help="the agreement study's directory name")
    sample.add_argument("--start", type=date.fromisoformat, required=True)
    sample.add_argument("--end", type=date.fromisoformat, required=True)
    sample.add_argument("--size", type=int, default=300)
    ask = commands.add_parser("agreement-ask", help="ask the sample of the --llm-* provider")
    ask.add_argument("--name", required=True)
    ask.add_argument("--label", required=True, help="name of this set of answers, e.g. A1")
    ask.add_argument("--rep", type=int, default=1)
    ask.add_argument("--concurrency", type=int, default=4)
    ask.add_argument(
        "--budget-usd", type=float, required=True, help="stop once the round's ledger reaches it"
    )
    ask.add_argument("--limit", type=int, default=None, help="ask only the first N (a pilot)")
    ask.add_argument("--round", default="go", help="ledger the spending goes to")
    agreement_report = commands.add_parser("agreement-report", help="compare the answers")
    agreement_report.add_argument("--name", required=True)
    agreement_report.add_argument("--label", action="append", required=True)
    agreement_report.add_argument("--round", default="go")
    agreement_report.add_argument(
        "--redraw",
        action="append",
        default=[],
        metavar="RUN[,RUN...]",
        help="run directories whose retriage.json are repeated draws (one group per option)",
    )
    agreement_report.add_argument("--output", type=Path)
    options = parser.parse_args(args)
    if getattr(options, "start", None) and options.start > options.end:
        parser.error("--start must not be after --end")
    if getattr(options, "lookback_days", None) is not None and options.lookback_days < 1:
        parser.error("--lookback-days must be at least 1")
    try:
        options.provider = _provider(options)
    except ValueError as error:
        parser.error(str(error))
    if options.command == "agreement-ask" and options.provider is None:
        parser.error("agreement-ask needs --llm-base-url, --llm-model and --llm-key-file")
    if options.command == "run" and not 1 <= options.concurrency <= runner.MAX_CONCURRENCY:
        parser.error(f"--concurrency must be between 1 and {runner.MAX_CONCURRENCY}")
    if options.command == "run":
        try:
            options.tracks = (*metrics.DEFAULT_TRACKS, *map(metrics.parse_track, options.track))
            options.skip_days = tuple(_skip_day(text) for text in options.skip_day)
        except ValueError as error:
            parser.error(str(error))
        if min(options.rep, options.triage_rep, options.via_rep) < 1:
            parser.error("--rep, --triage-rep and --via-rep start at 1")
        if options.via and options.stages != "retriage":
            parser.error("--via only applies to --stages retriage")
        if options.stages == "retriage" and options.base_variant is None:
            parser.error("--stages retriage needs --base-variant")
        if options.dry_run and options.stages != "retriage":
            parser.error("--dry-run only applies to --stages retriage")
    return options


def _provider(options: argparse.Namespace) -> LlmProvider | None:
    """The --llm-* provider, or None to use the one the production containers name."""
    given = [options.llm_base_url, options.llm_model, options.llm_key_file]
    if not any(given):
        return None
    if not all(given):
        raise ValueError("--llm-base-url, --llm-model and --llm-key-file go together")
    return LlmProvider(
        base_url=options.llm_base_url,
        model=options.llm_model,
        key_file=options.llm_key_file,
        extra_body=parse_extra_body(options.llm_extra_body),
        user_agent=options.llm_user_agent,
        session_header=options.llm_session_header or None,
    )


def _agreement_dir(options: argparse.Namespace) -> Path:
    return _environment(options).root / "agreement" / str(options.name)


def _agreement(options: argparse.Namespace) -> None:
    environment = _environment(options)
    directory = _agreement_dir(options)
    if options.command == "agreement-sample":
        source = snapshot.SourceDatabase(environment.source_container)
        sample = agreement.build_sample(
            source, start=options.start, end=options.end, size=options.size
        )
        population = source.scalar(
            "SELECT count(*) FROM newsroom_articles WHERE triage_status = 'done' AND "
            f"edition_date BETWEEN '{options.start}' AND '{options.end}'"
        )
        meta = {
            "start": options.start.isoformat(),
            "end": options.end.isoformat(),
            "size": len(sample),
            "population": int(population),
            "prompt_version": triage.load_triage_prompt(variant=BASELINE).version,
        }
        agreement.write_sample(directory / "sample.json", sample, meta)
        print(directory / "sample.json")
    elif options.command == "agreement-ask":
        meta, sample = agreement.read_sample(directory / "sample.json")
        if meta["prompt_version"] != triage.load_triage_prompt(variant=BASELINE).version:
            raise snapshot.ReplayError("the baseline triage prompt changed since the sample")
        chosen = sample[: options.limit] if options.limit else sample
        label = options.label
        started = datetime.now(UTC)
        ledger = costs.Ledger(
            environment.ledger_path(options.round), f"agreement-{options.name}-{label}"
        )
        answers: dict[str, agreement.Answer] = {}
        try:
            answers = asyncio.run(
                agreement.ask(
                    chosen,
                    cache=ResponseCache(environment.cache_dir),
                    ledger=ledger,
                    provider=options.provider,
                    rep=options.rep,
                    budget_usd=options.budget_usd,
                    concurrency=options.concurrency,
                )
            )
        finally:
            agreement.write_answers(
                directory / f"answers-{label}.json",
                answers,
                {
                    "label": label,
                    "rep": options.rep,
                    "provider": options.provider.describe(),
                    "asked": len(chosen),
                    "started_at": started.isoformat(timespec="seconds"),
                    "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
        total = costs.ledger_total(costs.read_ledger(ledger.path))
        print(f"{directory / f'answers-{label}.json'} (round {options.round}: {total})")
    else:
        meta, sample = agreement.read_sample(directory / "sample.json")
        runs = {
            label: agreement.read_answers(directory / f"answers-{label}.json")
            for label in options.label
        }
        redraws = None
        if options.redraw:
            groups = [
                [_run_path(options, name) / "retriage.json" for name in text.split(",")]
                for text in options.redraw
            ]
            draws = [
                [row for path in group for row in json.loads(path.read_text(encoding="utf-8"))]
                for group in groups
            ]
            redraws = agreement.redraw_noise(draws)
        text = agreement.report(
            meta,
            sample,
            runs,
            ledger=costs.read_ledger(environment.ledger_path(options.round)),
            redraws=redraws,
        )
        _emit(text, options.output)


def _snapshot(options: argparse.Namespace) -> snapshot.Snapshot:
    """The range, copied with as many earlier published days as the variants read."""
    lookback = options.lookback_days
    if lookback is None:
        names = [
            getattr(options, "variant", None),
            getattr(options, "base_variant", None),
            getattr(options, "via", None),
        ]
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
        server_container=options.server_container,
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
                        via_variant=get_variant(options.via) if options.via else None,
                        via_rep=options.via_rep,
                        select_mode=options.select,
                        dry_run=options.dry_run,
                        skip_days=options.skip_days,
                        round=options.round,
                        provider=options.provider,
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
        elif options.command.startswith("agreement-"):
            _agreement(options)
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
