"""Workstream ③: event analysis and per-market "why it matters" (spec §6.3).

Owner: editions worktree.
"""

from daily_insights_api.modules.newsroom.worker import Registration, Runtime


def register(runtime: Runtime) -> Registration:
    """Stage bindings for ``queue.ANALYSIS`` and ``queue.WHY``."""
    del runtime
    return Registration()
