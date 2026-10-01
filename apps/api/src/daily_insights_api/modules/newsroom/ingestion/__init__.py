"""Workstream ①: source polling, feed parsing, body fetch and quality checks, purge.

Owner: ingestion worktree (docs/specs/newsroom-pipeline.md §6.1, §8).
"""

from datetime import timedelta

from daily_insights_api.modules.newsroom import queue
from daily_insights_api.modules.newsroom.ingestion.deps import IngestionDeps
from daily_insights_api.modules.newsroom.ingestion.fetching import make_fetch_handler
from daily_insights_api.modules.newsroom.ingestion.polling import HostThrottle, run_poll_cycle
from daily_insights_api.modules.newsroom.ingestion.purge import purge_old_bodies
from daily_insights_api.modules.newsroom.worker import (
    PeriodicTask,
    Registration,
    Runtime,
    StageBinding,
)

DEFAULT_FETCH_CONCURRENCY = 4
POLL_EVERY = timedelta(minutes=1)
PURGE_EVERY = timedelta(minutes=10)


def fetch_concurrency(runtime: Runtime) -> int:
    """``newsroom_fetch_concurrency`` when the setting exists, else 4."""
    value = getattr(runtime.settings, "newsroom_fetch_concurrency", DEFAULT_FETCH_CONCURRENCY)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return DEFAULT_FETCH_CONCURRENCY
    return value


def register(runtime: Runtime, deps: IngestionDeps | None = None) -> Registration:
    """Stage bindings (``queue.FETCH``) and periodic tasks (source polling, 03:00 purge)."""
    resolved = deps or IngestionDeps()
    throttle = HostThrottle()

    async def poll() -> None:
        await run_poll_cycle(
            runtime.session_factory, runtime.settings, runtime.notifier, resolved, throttle
        )

    async def purge() -> None:
        await purge_old_bodies(runtime.session_factory, resolved.clock())

    return Registration(
        stages=[
            StageBinding(
                stage=queue.FETCH,
                handler=make_fetch_handler(runtime.settings, resolved),
                concurrency=fetch_concurrency(runtime),
            )
        ],
        periodic=[
            PeriodicTask(name="newsroom_poll_sources", interval=POLL_EVERY, run=poll),
            PeriodicTask(name="newsroom_purge_bodies", interval=PURGE_EVERY, run=purge),
        ],
    )
