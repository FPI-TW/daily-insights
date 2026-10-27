"""Recover the newsroom pipeline after a provider outage (runbook "重新排入").

Every command only counts what it would change unless ``--apply`` is given; the
dry run holds a read-only transaction. Run it where the newsroom worker runs
(``make newsroom-recover`` locally, ``docker exec`` on the worker in
production) once the provider is fixed:

- ``requeue``: put ``failed`` rows of one stage back on their queue, by error
  code and edition date. A failed triage without a usable embedding goes back
  to ``embed`` first.
- ``reembed-untranslated``: embed again the untriaged articles whose title
  translation failed, so they were embedded as written; triage follows.
- ``reopen``: return a published edition without any live item to draft so
  assembly can rebuild it; the next sweep publishes it once it has items.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings, get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.newsroom import clock, publishing, recovery
from daily_insights_api.modules.newsroom.models import MARKET_CODES, NewsroomEdition


def _deadline(value: str) -> datetime:
    """``HH:MM`` today in Taipei, or an ISO date-time (Taipei unless it has an offset)."""
    try:
        return clock.at_taipei(clock.taipei_today(), time.fromisoformat(value))
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"expected HH:MM or an ISO date-time, got {value!r}"
        ) from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=clock.TAIPEI)
    return moment.astimezone(UTC)


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recover the newsroom pipeline; dry run unless --apply"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--apply", action="store_true", help="write the changes")
        return sub

    def date_range(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--from", dest="start", type=date.fromisoformat, help="first edition date")
        sub.add_argument("--to", dest="end", type=date.fromisoformat, help="last edition date")

    requeue = command("requeue", "requeue failed rows of one stage")
    requeue.add_argument("--stage", required=True, choices=sorted(recovery.REQUEUE_STAGES))
    requeue.add_argument(
        "--error-code",
        dest="error_codes",
        action="append",
        required=True,
        help="error code to requeue (repeatable), e.g. triage_provider_http_402",
    )
    date_range(requeue)

    reembed = command("reembed-untranslated", "embed again articles whose translation failed")
    date_range(reembed)

    reopen = command("reopen", "return published editions to draft for re-assembly")
    reopen.add_argument("--edition-date", required=True, type=date.fromisoformat)
    reopen.add_argument("--market", choices=MARKET_CODES, help="one market (default: all)")
    reopen.add_argument(
        "--force", action="store_true", help="also reopen an edition that has live items"
    )
    reopen.add_argument(
        "--extend-late-fill",
        dest="late_fill_deadline",
        type=_deadline,
        help="new late-fill deadline: HH:MM today (Taipei) or an ISO date-time",
    )
    reopen.add_argument(
        "--actor", help="admin email recorded in newsroom_edit_log (no entry without it)"
    )

    options = parser.parse_args(args)
    start, end = getattr(options, "start", None), getattr(options, "end", None)
    if start is not None and end is not None and start > end:
        parser.error("--from must not be after --to")
    return options


async def _actor_id(database: AsyncSession, email: str) -> uuid.UUID:
    user = (
        await database.scalars(select(User).where(User.email == email.strip().lower()))
    ).one_or_none()
    if user is None or user.system_role != SystemRole.ADMIN or user.status != UserStatus.ACTIVE:
        raise RuntimeError(f"{email} is not an active admin")
    return user.id


def _taipei(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(clock.TAIPEI).isoformat()


async def _reopen(
    database: AsyncSession, options: argparse.Namespace, now: datetime
) -> tuple[bool, dict[str, Any]]:
    query = select(NewsroomEdition).where(NewsroomEdition.edition_date == options.edition_date)
    if options.market:
        query = query.where(NewsroomEdition.market_code == options.market)
    query = query.order_by(NewsroomEdition.market_code)
    if options.apply:
        query = query.with_for_update()
    editions = (await database.scalars(query)).all()
    # Checked in the dry run too, so a mistyped email shows before --apply.
    user_id = await _actor_id(database, options.actor) if options.actor else None
    checks = [
        await publishing.check_reopen(
            database,
            edition,
            force=options.force,
            late_fill_deadline=options.late_fill_deadline,
            now=now,
        )
        for edition in editions
    ]
    ok = bool(checks) and all(check.action != "refuse" for check in checks)
    if options.apply and ok:
        for check in checks:
            if check.action == "reopen":
                await publishing.reopen_edition(
                    database,
                    check.edition_id,
                    force=options.force,
                    late_fill_deadline=options.late_fill_deadline,
                    user_id=user_id,
                    now=now,
                )
    report: dict[str, Any] = {
        "edition_date": options.edition_date.isoformat(),
        "applied": options.apply and ok,
        "edit_log": bool(options.actor),
        "editions": [
            {
                "market": check.market_code,
                "status": check.status,
                "live_items": check.live_items,
                "abandoned_items": check.abandoned_items,
                "late_fill_deadline": _taipei(check.late_fill_deadline),
                "new_late_fill_deadline": _taipei(check.new_late_fill_deadline),
                "action": check.action,
                "reason": check.reason,
            }
            for check in checks
        ],
    }
    if not checks:
        report["error"] = "no edition on that date"
    elif not ok:
        report["error"] = "an edition cannot be reopened as asked; nothing was changed"
    elif any(check.action == "reopen" for check in checks):
        report["next"] = (
            "re-assemble the date (make assemble-newsroom "
            f"EDITION_DATE={options.edition_date.isoformat()}); the next sweep publishes "
            "each market that has an item"
        )
    return ok, report


async def run(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    options: argparse.Namespace,
    *,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run one command in one transaction; returns the exit code and the report."""
    moment = now or datetime.now(UTC)
    report: dict[str, Any] = {
        "command": options.command,
        "mode": "apply" if options.apply else "dry_run",
    }
    async with session_factory() as database:
        if not options.apply:
            await database.execute(text("SET TRANSACTION READ ONLY"))
        if options.command == "reopen":
            ok, details = await _reopen(database, options, moment)
            report |= details
        else:
            ok = True
            span = {
                "from": options.start.isoformat() if options.start else None,
                "to": options.end.isoformat() if options.end else None,
            }
            if options.command == "requeue":
                plan = await recovery.requeue(
                    database,
                    options.stage,
                    options.error_codes,
                    start=options.start,
                    end=options.end,
                    embedding_model=settings.newsroom_embedding_model,
                    apply=options.apply,
                    now=moment,
                )
                report |= {"stage": options.stage, "error_codes": options.error_codes}
            else:
                plan = await recovery.reembed_untranslated(
                    database,
                    start=options.start,
                    end=options.end,
                    embedding_model=settings.newsroom_embedding_model,
                    apply=options.apply,
                    now=moment,
                )
                report["embedding_model"] = settings.newsroom_embedding_model
            report |= span | plan.as_dict()
        if options.apply and ok:
            await database.commit()
        else:
            await database.rollback()
    if not options.apply and ok:
        report["hint"] = "dry run: nothing was written; add --apply to write"
    return (0 if ok else 1), report


async def recover(options: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        return await run(create_session_factory(engine), settings, options)
    finally:
        await engine.dispose()


def main(args: Sequence[str] | None = None) -> int:
    options = parse_args(args)
    try:
        code, report = asyncio.run(recover(options))
    except Exception as error:
        print(f"newsroom recovery failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
