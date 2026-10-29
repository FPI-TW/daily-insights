"""Fence newsroom claims independently of retry accounting; retain legacy sources.

Revision ID: 20261029_0054
Revises: 20261027_0053

The corrected legacy import (0034) owns legacy_sources. Databases that already
ran the original import need the column added here and their surviving article
links copied into it. Links already lost by the old import cannot be inferred;
they must be restored from a pre-cutover backup, never guessed from headlines.
Downgrade retains this import-owned column and removes only the claim tokens.
"""

import logging
from collections.abc import Sequence

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


def upgrade() -> None:
    for table, stages in STAGES.items():
        for stage in stages:
            op.add_column(table, sa.Column(f"{stage}_claim_token", postgresql.UUID(as_uuid=True)))
    op.execute(
        "ALTER TABLE newsroom_events ADD COLUMN IF NOT EXISTS "
        "legacy_sources jsonb NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute(
        """
        UPDATE newsroom_events AS e
        SET legacy_sources = links.sources
        FROM (
            SELECT a.event_id, jsonb_agg(jsonb_build_object(
                'name', CASE WHEN s.kind = 'manual' THEN split_part(a.url, '/', 3)
                             ELSE s.name END,
                'url', a.url, 'published_at', a.published_at
            ) ORDER BY s.trust_tier DESC, a.published_at DESC NULLS LAST,
                       a.first_seen_at, a.id) AS sources
            FROM newsroom_articles AS a
            JOIN newsroom_sources AS s ON s.id = a.source_id
            GROUP BY a.event_id
        ) AS links
        WHERE e.id = links.event_id AND e.created_by = 'legacy'
          AND e.legacy_sources = '[]'::jsonb
        """
    )
    missing = op.get_bind().scalar(
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
