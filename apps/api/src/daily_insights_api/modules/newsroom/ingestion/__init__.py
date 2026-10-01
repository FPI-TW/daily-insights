"""Workstream ①: source polling, feed parsing, body fetch and quality checks, purge.

Owner: ingestion worktree (docs/specs/newsroom-pipeline.md §6.1, §8).
"""

from daily_insights_api.modules.newsroom.worker import Registration, Runtime


def register(runtime: Runtime) -> Registration:
    """Stage bindings (``queue.FETCH``) and periodic tasks (source polling, 03:00 purge)."""
    del runtime
    return Registration()
