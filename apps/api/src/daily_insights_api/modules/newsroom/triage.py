"""Workstream ②: embedding and per-article triage with event assignment (spec §6.2).

Owner: triage worktree.
"""

from daily_insights_api.modules.newsroom.worker import Registration, Runtime


def register(runtime: Runtime) -> Registration:
    """Stage bindings for ``queue.EMBED`` and ``queue.TRIAGE``."""
    del runtime
    return Registration()
