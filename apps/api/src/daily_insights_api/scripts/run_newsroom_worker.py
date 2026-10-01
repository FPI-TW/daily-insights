import asyncio
import sys

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.newsroom.worker import (
    NewsroomWorker,
    Runtime,
    collect_registrations,
)


async def run_worker() -> None:
    settings = get_settings()
    if settings.runtime_role != "newsroom-worker":
        raise RuntimeError("runtime role must be newsroom-worker")
    if not settings.newsroom_enabled:
        raise RuntimeError("newsroom worker requires DAILY_INSIGHTS_NEWSROOM_ENABLED=true")
    engine = create_engine(settings)
    runtime = Runtime.build(settings, create_session_factory(engine))
    try:
        await NewsroomWorker(runtime, collect_registrations(runtime)).run()
    finally:
        await runtime.aclose()
        await engine.dispose()


def main() -> int:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f"newsroom worker failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
