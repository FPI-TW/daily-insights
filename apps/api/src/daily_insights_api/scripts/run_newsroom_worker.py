import asyncio
import sys

from anyio import Path

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import Settings, get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.modules.newsroom import worker as newsroom_worker
from daily_insights_api.modules.newsroom.worker import (
    NewsroomWorker,
    Runtime,
    collect_registrations,
)

HEARTBEAT_PATH = Path(newsroom_worker.HEARTBEAT_PATH)


async def idle(settings: Settings, *, once: bool = False) -> None:
    """Stay healthy while the pipeline is disabled, touching nothing but the heartbeat.

    Compose starts the worker by default, so a disabled pipeline must not exit
    (``restart: unless-stopped`` would loop it) and must not reach the database
    or the providers.
    """
    print(
        "newsroom worker idle: DAILY_INSIGHTS_NEWSROOM_ENABLED is false",
        file=sys.stderr,
        flush=True,
    )
    while True:
        await HEARTBEAT_PATH.touch()
        if once:
            return
        await asyncio.sleep(settings.newsroom_worker_poll_seconds)


async def run_worker(*, once: bool = False) -> None:
    settings = get_settings()
    if settings.runtime_role != "newsroom-worker":
        raise RuntimeError("runtime role must be newsroom-worker")
    if not settings.newsroom_enabled:
        await idle(settings, once=once)
        return
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
