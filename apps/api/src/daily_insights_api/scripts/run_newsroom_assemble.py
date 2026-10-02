"""Assemble the newsroom drafts for one edition date (spec §6.3), once.

The daily routine runs the same assembly at 08:00 through the orchestration
``newsroom_assemble`` function; this entry point is for local verification and
for re-running a day by hand. Assembly is idempotent: an edition that already
exists is rebuilt only while it is still a draft.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.newsroom.api import (
    Runtime,
    assemble_editions,
    edition_window,
    taipei_today,
)


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assemble the newsroom drafts for one date")
    parser.add_argument(
        "--edition-date",
        type=date.fromisoformat,
        help="edition date (YYYY-MM-DD); defaults to today in Asia/Taipei",
    )
    return parser.parse_args(args)


async def assemble(edition_date: date) -> dict[str, object]:
    settings = get_settings()
    if not settings.newsroom_enabled:
        raise RuntimeError("newsroom assembly requires DAILY_INSIGHTS_NEWSROOM_ENABLED=true")
    if datetime.now(UTC) < edition_window(edition_date)[1]:
        raise RuntimeError(
            f"the {edition_date.isoformat()} collection window closes at 08:00 Asia/Taipei"
        )
    engine = create_engine(settings)
    runtime = Runtime.build(settings, create_session_factory(engine))
    try:
        report = await assemble_editions(runtime, edition_date)
    finally:
        await runtime.aclose()
        await engine.dispose()
    return report.as_dict()


def main(args: list[str] | None = None) -> int:
    options = parse_args(args)
    try:
        report = asyncio.run(assemble(options.edition_date or taipei_today()))
    except Exception as error:
        print(f"newsroom assembly failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
