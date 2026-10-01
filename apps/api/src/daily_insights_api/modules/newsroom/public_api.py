"""Workstream ⑤: reader endpoints under /api/newsroom (spec §6.5).

Owner: reader worktree. Mounted by web/app.py.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/newsroom", tags=["newsroom"])
