"""Fence newsroom claims independently of retry accounting; retain legacy sources.

Revision ID: 20261029_0054
Revises: 20261027_0053

The corrected legacy import (0034) owns legacy_sources. Databases that already
ran the original import need the column added here and the links that import
attached copied into it: articles that existed when the legacy event was
created, not ones triage or a merge brought in later. The legacy tables (and
their outlet names) were dropped in 0035, so a manual-source link is named the
way readers already saw it, by the frozen copy of the reader's hostname rule.
Links already lost by the old import cannot be inferred; they must be restored
from a pre-cutover backup, never guessed from headlines. Downgrade retains this
import-owned column and removes only the claim tokens.
"""

import logging
from collections import defaultdict
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261029_0054"
down_revision: str | None = "20261027_0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STAGES = {
    "newsroom_articles": ("fetch", "embed", "triage"),
    "newsroom_events": ("analysis", "en"),
    "newsroom_edition_items": ("why",),
}

events = sa.table(
    "newsroom_events",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("legacy_sources", postgresql.JSONB()),
)

# Only legacy events still without a snapshot, and only the articles the import
# created or linked (same transaction or earlier), in the reader's link order.
_IMPORTED_LINKS = sa.text(
    """
    SELECT a.event_id, a.url, a.published_at, s.kind, s.name
    FROM newsroom_events AS e
    JOIN newsroom_articles AS a ON a.event_id = e.id
    JOIN newsroom_sources AS s ON s.id = a.source_id
    WHERE e.created_by = 'legacy' AND e.legacy_sources = '[]'::jsonb
      AND a.created_at <= e.created_at
    ORDER BY a.event_id, s.trust_tier DESC, a.published_at DESC NULLS LAST,
             a.first_seen_at, a.id
    """
)


def _reader_name(kind: str, name: str, url: str) -> str:
    """Frozen copy of ``public_api._sources``' naming: a manual link shows its site."""
    if kind == "manual":
        return urlsplit(url).hostname or name
    return name


def upgrade() -> None:
    for table, stages in STAGES.items():
        for stage in stages:
            op.add_column(table, sa.Column(f"{stage}_claim_token", postgresql.UUID(as_uuid=True)))
    op.execute(
        "ALTER TABLE newsroom_events ADD COLUMN IF NOT EXISTS "
        "legacy_sources jsonb NOT NULL DEFAULT '[]'::jsonb"
    )
    bind = op.get_bind()
    links: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in bind.execute(_IMPORTED_LINKS):
        links[row.event_id].append(
            {
                "name": _reader_name(row.kind, row.name, row.url),
                "url": row.url,
                "published_at": (
                    row.published_at.isoformat() if row.published_at is not None else None
                ),
            }
        )
    if links:
        bind.execute(
            events.update()
            .where(events.c.id == sa.bindparam("event_id"))
            .values(legacy_sources=sa.bindparam("links", type_=postgresql.JSONB())),
            [{"event_id": event_id, "links": sources} for event_id, sources in links.items()],
        )
    missing = bind.scalar(
        sa.text(
            "SELECT count(*) FROM newsroom_events "
            "WHERE created_by = 'legacy' AND legacy_sources = '[]'::jsonb"
        )
    )
    if missing:
        logging.getLogger("alembic.runtime.migration").warning(
            "%s legacy newsroom events have no recoverable source link; "
            "restore their legacy_sources from a pre-cutover backup",
            missing,
        )


def downgrade() -> None:
    for table, stages in reversed(STAGES.items()):
        for stage in reversed(stages):
            op.drop_column(table, f"{stage}_claim_token")
