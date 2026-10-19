"""Re-sign current English against the headline and summary only.

Revision ID: 20261016_0047
Revises: 20261016_0046

"Why it matters" is now shown in the admin console only, so the English shown
to readers no longer depends on it: ``en_source_digest`` covers the zh-hant
headline and summary alone. An event whose stored digest still matches its
current text under the old formula (headline, summary and the "why" of every
reader-visible item) gets the new digest, so its English stays visible instead
of waiting for a re-translation. Any other event was stale already and is left
for the translation stage.
"""

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261016_0047"
down_revision: str | None = "20261016_0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _digest(payload: dict[str, object]) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resign(old_whys_visible: bool) -> None:
    """Swap digests from one formula to the other where the source still matches."""
    connection = op.get_bind()
    events = connection.execute(
        sa.text(
            "SELECT id, headline_zh_hant, summary_zh_hant, en_source_digest "
            "FROM newsroom_events WHERE en_source_digest IS NOT NULL"
        )
    ).all()
    for event_id, headline, summary, stored in events:
        whys = {
            str(item_id): why
            for item_id, why in connection.execute(
                sa.text(
                    "SELECT i.id, i.why_zh_hant FROM newsroom_edition_items i "
                    "JOIN newsroom_editions e ON e.id = i.edition_id "
                    "WHERE i.event_id = :event_id AND e.status = 'published' "
                    "AND i.why_status = 'ready' AND i.removed_at IS NULL "
                    "AND i.hidden_at IS NULL AND i.abandoned_at IS NULL"
                ),
                {"event_id": event_id},
            )
        }
        with_whys = _digest({"headline": headline, "summary": summary, "whys": whys})
        without = _digest({"headline": headline, "summary": summary})
        source, target = (with_whys, without) if old_whys_visible else (without, with_whys)
        if stored == source:
            connection.execute(
                sa.text("UPDATE newsroom_events SET en_source_digest = :digest WHERE id = :id"),
                {"digest": target, "id": event_id},
            )


def upgrade() -> None:
    _resign(old_whys_visible=True)


def downgrade() -> None:
    _resign(old_whys_visible=False)
