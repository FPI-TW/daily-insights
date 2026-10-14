"""Re-embed the stored articles of a range of edition dates, once.

Run it right after the embedding model or the translation step changes, for
the edition that is still collecting (default: the next 08:00 edition), so new
articles are compared with vectors of the same kind. Past editions only need it
for the quality report's cross-day repeat check.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.newsroom.clock import edition_date_for
from daily_insights_api.modules.newsroom.reembed import reembed_articles
from daily_insights_api.modules.newsroom.worker import Runtime


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Re-embed newsroom articles")
    parser.add_argument("--start", type=date.fromisoformat, help="first edition date")
    parser.add_argument("--end", type=date.fromisoformat, help="last edition date (inclusive)")
    options = parser.parse_args(args)
    collecting = edition_date_for(datetime.now(UTC))
    options.start = options.start or collecting
    options.end = options.end or max(options.start, collecting)
    if options.start > options.end:
        parser.error("--start must not be after --end")
    return options


async def reembed(start: date, end: date) -> dict[str, int]:
    settings = get_settings()
    if not settings.newsroom_enabled:
        raise RuntimeError("re-embedding requires DAILY_INSIGHTS_NEWSROOM_ENABLED=true")
    engine = create_engine(settings)
    runtime = Runtime.build(settings, create_session_factory(engine))
    try:
        report = await reembed_articles(runtime, start, end)
    finally:
        await runtime.aclose()
        await engine.dispose()
    return report.as_dict()


def main(args: list[str] | None = None) -> int:
    options = parse_args(args)
    try:
        report = asyncio.run(reembed(options.start, options.end))
    except Exception as error:
        print(f"newsroom re-embedding failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
