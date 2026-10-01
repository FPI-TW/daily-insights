"""Workstream ④: admin endpoints under /api/admin/newsroom (spec §6.4).

Owner: admin worktree. Mounted by web/app.py.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/admin/newsroom", tags=["newsroom management"])
