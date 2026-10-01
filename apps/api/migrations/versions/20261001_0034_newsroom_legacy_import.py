"""Move the legacy daily-news history into the newsroom tables.

Revision ID: 20261001_0034
Revises: 20261001_0033

Data only (docs/specs/newsroom-pipeline.md D20, §9). For every edition date and
market the newest ``complete``/``partial`` legacy revision that still has a
reader-visible item becomes a published ``legacy`` newsroom edition. Visible
``news_items`` rows of one date that share a URL become one ``legacy`` event
(spec D9): its three-language headline and summary come from the first market
in global → tw_equity → us_equity order (``news_presentations``; missing zh-hans
is converted from zh-hant with OpenCC ``tw2sp``). Each market edition gets its
own ``legacy`` item without a "why", keeping that market's rank and stars, and
the event gets one ``manual``-source article that carries the source link.
Dates that already have a newsroom edition for the market are skipped, so new
pipeline output is never overwritten.

The English digest and the publication times are computed by frozen local copies
of ``translation.zh_hant_digest`` and ``clock`` so later edits to the module can
never change what this migration wrote.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import opencc
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261001_0034"
down_revision: str | None = "20261001_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TAIPEI = ZoneInfo("Asia/Taipei")
AUTO_PUBLISH = time(9, 0)
LATE_FILL_DEADLINE = time(12, 0)
HEADLINE_MAX_CHARS = 500
TITLE_MAX_CHARS = 1_000
LOCALES = ("zh-hant", "zh-hans", "en")
# A story in several market editions takes the text of the first market here.
MARKET_ORDER = ("global", "tw_equity", "us_equity")
# Marks the articles this migration created, so the downgrade can tell them
# from pre-existing articles it only linked to a legacy event.
LEGACY_BODY_REASON = "legacy_import"

editions = sa.table(
    "newsroom_editions",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("edition_date", sa.Date()),
    sa.column("market_code", sa.String()),
    sa.column("status", sa.String()),
    sa.column("selection_mode", sa.String()),
    sa.column("auto_publish_at", sa.DateTime(timezone=True)),
    sa.column("late_fill_deadline", sa.DateTime(timezone=True)),
    sa.column("assembled_at", sa.DateTime(timezone=True)),
    sa.column("published_at", sa.DateTime(timezone=True)),
    sa.column("late_fill_closed_at", sa.DateTime(timezone=True)),
)
events = sa.table(
    "newsroom_events",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("edition_date", sa.Date()),
    sa.column("working_title", sa.String()),
    sa.column("status", sa.String()),
    sa.column("created_by", sa.String()),
    sa.column("headline_zh_hant", sa.String()),
    sa.column("summary_zh_hant", sa.Text()),
    sa.column("headline_zh_hans", sa.String()),
    sa.column("summary_zh_hans", sa.Text()),
    sa.column("headline_en", sa.String()),
    sa.column("summary_en", sa.Text()),
    sa.column("related_symbols", postgresql.JSONB()),
    sa.column("analysis_status", sa.String()),
    sa.column("analysis_model", sa.String()),
    sa.column("analyzed_at", sa.DateTime(timezone=True)),
    sa.column("en_status", sa.String()),
    sa.column("en_source_digest", sa.String()),
)
items = sa.table(
    "newsroom_edition_items",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("edition_id", postgresql.UUID(as_uuid=True)),
    sa.column("event_id", postgresql.UUID(as_uuid=True)),
    sa.column("rank", sa.Integer()),
    sa.column("stars", sa.Integer()),
    sa.column("origin", sa.String()),
    sa.column("why_status", sa.String()),
    sa.column("why_en_status", sa.String()),
)
articles = sa.table(
    "newsroom_articles",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("source_id", postgresql.UUID(as_uuid=True)),
    sa.column("url", sa.Text()),
    sa.column("url_hash", sa.String()),
    sa.column("title", sa.String()),
    sa.column("published_at", sa.DateTime(timezone=True)),
    sa.column("first_seen_at", sa.DateTime(timezone=True)),
    sa.column("edition_date", sa.Date()),
    sa.column("body", sa.Text()),
    sa.column("body_status", sa.String()),
    sa.column("body_quality_reason", sa.String()),
    sa.column("fetch_status", sa.String()),
    sa.column("embed_status", sa.String()),
    sa.column("triage_status", sa.String()),
    sa.column("relevant", sa.Boolean()),
    sa.column("topic", sa.String()),
    sa.column("market_scores", postgresql.JSONB()),
    sa.column("event_id", postgresql.UUID(as_uuid=True)),
    sa.column("triaged_at", sa.DateTime(timezone=True)),
)

# The newest publishable legacy revision per date and market, restricted to
# items a legacy reader could still see: not hidden, and not suppressed by a
# hidden item with the same URL or event key in any edition of the market
# (frozen copy of ``news/curation.py::visible_item``).
_VISIBLE_ITEMS = sa.text(
    """
    WITH visible AS (
        SELECT item.*, edition.edition_date, edition.market_code, edition.revision,
               edition.generated_at, edition.model_name
        FROM news_items AS item
        JOIN news_editions AS edition ON edition.id = item.edition_id
        WHERE edition.status IN ('complete', 'partial')
          AND item.hidden_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM news_items AS hidden
              JOIN news_editions AS hidden_edition ON hidden_edition.id = hidden.edition_id
              WHERE hidden_edition.market_code = edition.market_code
                AND hidden.hidden_at IS NOT NULL
                AND (
                    hidden.source_url = item.source_url
                    OR (hidden.event_key IS NOT NULL AND hidden.event_key = item.event_key)
                )
          )
    ),
    chosen AS (
        SELECT DISTINCT ON (edition_date, market_code) edition_id
        FROM visible
        ORDER BY edition_date, market_code, revision DESC
    )
    SELECT visible.id, visible.edition_id, visible.edition_date, visible.market_code,
           visible.generated_at, visible.model_name, visible.rank, visible.topic,
           visible.source_url, visible.source_headline, visible.source_published_at,
           visible.importance
    FROM visible
    JOIN chosen ON chosen.edition_id = visible.edition_id
    WHERE NOT EXISTS (
        SELECT 1 FROM newsroom_editions AS existing
        WHERE existing.edition_date = visible.edition_date
          AND existing.market_code = visible.market_code
    )
    ORDER BY visible.edition_date, visible.market_code, visible.rank
    """
)


def _at_taipei(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=TAIPEI)


def _zh_hant_digest(
    headline: str | None, summary: str | None, whys: Mapping[str, str | None]
) -> str:
    """Frozen copy of ``newsroom.translation.zh_hant_digest``."""
    payload = json.dumps(
        {"headline": headline, "summary": summary, "whys": dict(whys)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _presentations(bind: sa.Connection, item_ids: list[uuid.UUID]) -> dict[Any, dict[str, Any]]:
    by_item: dict[Any, dict[str, Any]] = {}
    if not item_ids:
        return by_item
    rows = bind.execute(
        sa.text(
            "SELECT item_id, locale, headline, summary FROM news_presentations "
            "WHERE item_id = ANY(:ids)"
        ),
        {"ids": item_ids},
    )
    for row in rows:
        by_item.setdefault(row.item_id, {})[row.locale] = row
    return by_item


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _group_by_story(rows: Sequence[Any]) -> list[list[Any]]:
    """Legacy items of one date sharing a URL become one event (spec D9).

    Rows are visited global → tw_equity → us_equity, then by rank, so each
    group's first row is the one whose text the event takes. A market keeps a
    single item per event: a repeated URL in the same edition keeps the
    better-ranked row.
    """
    groups: dict[tuple[date, str], list[Any]] = {}
    ordered = sorted(
        rows, key=lambda row: (row.edition_date, MARKET_ORDER.index(row.market_code), row.rank)
    )
    for row in ordered:
        group = groups.setdefault((row.edition_date, _url_hash(row.source_url)), [])
        if all(member.edition_id != row.edition_id for member in group):
            group.append(row)
    return list(groups.values())


def upgrade() -> None:
    bind = op.get_bind()
    legacy_items = bind.execute(_VISIBLE_ITEMS).all()
    if not legacy_items:
        return
    manual_source_id = bind.scalar(
        sa.text("SELECT id FROM newsroom_sources WHERE key = 'manual' AND kind = 'manual'")
    )
    if manual_source_id is None:
        raise RuntimeError("the manual newsroom source is missing (migration 20261001_0032)")
    presentations = _presentations(bind, [row.id for row in legacy_items])
    # The reader never showed an item without its zh-hant text.
    shown = [row for row in legacy_items if "zh-hant" in presentations.get(row.id, {})]
    converter = opencc.OpenCC("tw2sp")
    edition_ids: dict[Any, uuid.UUID] = {}
    for group in _group_by_story(shown):
        item_ids: list[uuid.UUID] = []
        for row in group:
            if row.edition_id not in edition_ids:
                edition_ids[row.edition_id] = _import_edition(row)
            item_ids.append(uuid.uuid4())
        _import_story(
            bind,
            group,
            presentations[group[0].id],
            item_ids,
            [edition_ids[row.edition_id] for row in group],
            manual_source_id,
            converter,
        )


def _import_edition(row: Any) -> uuid.UUID:
    edition_id = uuid.uuid4()
    op.execute(
        editions.insert().values(
            id=edition_id,
            edition_date=row.edition_date,
            market_code=row.market_code,
            status="published",
            selection_mode="legacy",
            auto_publish_at=_at_taipei(row.edition_date, AUTO_PUBLISH),
            late_fill_deadline=_at_taipei(row.edition_date, LATE_FILL_DEADLINE),
            assembled_at=row.generated_at,
            published_at=row.generated_at,
            late_fill_closed_at=_at_taipei(row.edition_date, LATE_FILL_DEADLINE),
        )
    )
    return edition_id


def _import_story(
    bind: sa.Connection,
    group: Sequence[Any],
    texts: Mapping[str, Any],
    item_ids: Sequence[uuid.UUID],
    edition_ids: Sequence[uuid.UUID],
    manual_source_id: uuid.UUID,
    converter: opencc.OpenCC,
) -> None:
    """One event with the first row's text, one item per market edition, one article."""
    source = group[0]
    zh_hant = texts["zh-hant"]
    headline_zh_hant = _clip(zh_hant.headline, HEADLINE_MAX_CHARS)
    zh_hans = texts.get("zh-hans")
    english = texts.get("en")
    event_id = uuid.uuid4()
    # The reader recomputes this over the event's visible items; a legacy
    # item has no "why", so each contributes ``"<item id>": null``.
    digest = (
        _zh_hant_digest(
            headline_zh_hant, zh_hant.summary, {str(item_id): None for item_id in item_ids}
        )
        if english is not None
        else None
    )
    op.execute(
        events.insert().values(
            id=event_id,
            edition_date=source.edition_date,
            working_title=headline_zh_hant,
            status="open",
            created_by="legacy",
            headline_zh_hant=headline_zh_hant,
            summary_zh_hant=zh_hant.summary,
            headline_zh_hans=_clip(
                zh_hans.headline if zh_hans is not None else converter.convert(zh_hant.headline),
                HEADLINE_MAX_CHARS,
            ),
            summary_zh_hans=(
                zh_hans.summary if zh_hans is not None else converter.convert(zh_hant.summary)
            ),
            headline_en=_clip(english.headline, HEADLINE_MAX_CHARS) if english else None,
            summary_en=english.summary if english else None,
            related_symbols=[],
            analysis_status="ready",
            analysis_model=source.model_name,
            analyzed_at=source.generated_at,
            en_status="ready" if english is not None else "idle",
            en_source_digest=digest,
        )
    )
    for row, item_id, edition_id in zip(group, item_ids, edition_ids, strict=True):
        op.execute(
            items.insert().values(
                id=item_id,
                edition_id=edition_id,
                event_id=event_id,
                rank=row.rank,
                stars=row.importance,
                origin="legacy",
                why_status="ready",
                why_en_status="ready",
            )
        )
    url_hash = _url_hash(source.source_url)
    existing = bind.execute(
        sa.text("SELECT id, event_id FROM newsroom_articles WHERE url_hash = :hash"),
        {"hash": url_hash},
    ).first()
    if existing is not None:
        if existing.event_id is None:
            op.execute(
                articles.update().where(articles.c.id == existing.id).values(event_id=event_id)
            )
        return
    op.execute(
        articles.insert().values(
            id=uuid.uuid4(),
            source_id=manual_source_id,
            url=source.source_url,
            url_hash=url_hash,
            title=_clip(source.source_headline, TITLE_MAX_CHARS),
            published_at=source.source_published_at,
            first_seen_at=source.source_published_at or source.generated_at,
            edition_date=source.edition_date,
            body=None,
            body_status="purged",
            body_quality_reason=LEGACY_BODY_REASON,
            fetch_status="done",
            embed_status="idle",
            triage_status="done",
            relevant=True,
            topic=source.topic,
            market_scores={},
            event_id=event_id,
            triaged_at=source.generated_at,
        )
    )


def downgrade() -> None:
    legacy_events = "SELECT id FROM newsroom_events WHERE created_by = 'legacy'"
    op.execute(
        f"UPDATE newsroom_articles SET event_id = NULL WHERE event_id IN ({legacy_events}) "
        f"AND body_quality_reason IS DISTINCT FROM '{LEGACY_BODY_REASON}'"
    )
    op.execute(f"DELETE FROM newsroom_articles WHERE body_quality_reason = '{LEGACY_BODY_REASON}'")
    op.execute(
        "DELETE FROM newsroom_edition_items WHERE origin = 'legacy' "
        f"OR event_id IN ({legacy_events}) "
        "OR edition_id IN (SELECT id FROM newsroom_editions WHERE selection_mode = 'legacy')"
    )
    op.execute("DELETE FROM newsroom_events WHERE created_by = 'legacy'")
    op.execute("DELETE FROM newsroom_editions WHERE selection_mode = 'legacy'")
