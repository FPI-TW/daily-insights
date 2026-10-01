"""Workstream ③: 08:00 edition assembly — scoring, editor pass, quotas (spec §6.3).

Owner: editions worktree. Triggered by the ``newsroom_assemble`` orchestration
function; must be idempotent per edition date.
"""

from datetime import date

from daily_insights_api.modules.newsroom.worker import Runtime


async def assemble_editions(runtime: Runtime, edition_date: date) -> None:
    raise NotImplementedError
