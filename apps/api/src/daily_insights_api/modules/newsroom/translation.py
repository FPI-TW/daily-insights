"""Workstream ③: OpenCC zh-hans conversion and async English translation (spec D7).

Owner: editions worktree.
"""

from daily_insights_api.modules.newsroom.worker import Registration, Runtime


def register(runtime: Runtime) -> Registration:
    """Stage binding for ``queue.TRANSLATE``."""
    del runtime
    return Registration()
