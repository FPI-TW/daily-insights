"""Run the unified provider/function/job worker."""

import argparse
import asyncio
import os
import socket
import uuid

from anyio import Path

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.config import get_settings
from daily_insights_api.core.database import create_engine, create_session_factory
from daily_insights_api.core.logging import configure_logging
from daily_insights_api.modules.orchestration.functions import build_function_handlers
from daily_insights_api.modules.orchestration.projections import (
    claim_ready_projection,
    execute_projection,
)
from daily_insights_api.modules.orchestration.worker import (
    ClaimedFunction,
    FunctionOutcome,
    claim_ready_function,
    execute_claimed,
    reconcile_function_jobs,
)

HEARTBEAT_PATH = Path("/tmp/orchestration-worker-heartbeat")


async def worker_loop(*, once: bool = False) -> None:
    settings = get_settings()
    if not settings.orchestration_enabled:
        while True:
            await HEARTBEAT_PATH.touch()
            if once:
                return
            await asyncio.sleep(settings.orchestration_poll_seconds)
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    handlers = build_function_handlers(settings, sessions)
    owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
    tasks: set[asyncio.Task[None]] = set()
    claimed_count = 0
    prefer_projection = True
    try:
        while True:
            await HEARTBEAT_PATH.touch()
            tasks = {task for task in tasks if not task.done()}
            available = max(0, settings.orchestration_worker_concurrency - len(tasks))
            made_progress = False
            for _ in range(available):
                projection = None
                claimed = None
                if prefer_projection:
                    projection = await claim_ready_projection(sessions, owner=owner)
                    if projection is None:
                        claimed = await claim_ready_function(engine, sessions, owner=owner)
                else:
                    claimed = await claim_ready_function(engine, sessions, owner=owner)
                    if claimed is None:
                        projection = await claim_ready_projection(sessions, owner=owner)
                prefer_projection = not prefer_projection
                if projection is not None:
                    tasks.add(
                        asyncio.create_task(
                            execute_projection(
                                sessions,
                                job_run_id=projection.job_run_id,
                                owner=owner,
                                fence_token=projection.fence_token,
                            )
                        )
                    )
                    claimed_count += 1
                    made_progress = True
                    continue
                if claimed is not None:
                    handler = handlers.get(claimed.function_key)
                    if handler is None:

                        async def missing(current: ClaimedFunction) -> FunctionOutcome:
                            raise RuntimeError(f"no handler for {current.function_key}")

                        handler = missing
                    run = execute_claimed(claimed, sessions, handler)
                    tasks.add(asyncio.create_task(run))
                    claimed_count += 1
                    made_progress = True
                    continue
                break
            await reconcile_function_jobs(sessions)
            if once and (claimed_count or not tasks):
                break
            if not made_progress:
                if tasks:
                    done, _ = await asyncio.wait(
                        tasks,
                        timeout=settings.orchestration_poll_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        await task
                else:
                    await asyncio.sleep(settings.orchestration_poll_seconds)
        if tasks:
            await asyncio.gather(*tasks)
        await reconcile_function_jobs(sessions)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await handlers.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run unified orchestration worker")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    configure_logging()
    asyncio.run(worker_loop(once=args.once))


if __name__ == "__main__":
    main()
