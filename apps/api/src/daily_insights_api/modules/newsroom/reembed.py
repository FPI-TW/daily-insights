"""Re-embed stored articles with the current embedding model and translation.

Vectors from two embedding models are not comparable, so after the model (or
the translation step) changes, every article of a window that is still
collecting must be embedded again before new articles are compared with it.
Event assignments, scores and editions are left as they are.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, update

from daily_insights_api.modules.newsroom.models import NewsroomArticle
from daily_insights_api.modules.newsroom.providers import CallAudit
from daily_insights_api.modules.newsroom.triage import (
    embedding_input,
    needs_translation,
    translated_embedding_input,
)
from daily_insights_api.modules.newsroom.worker import Runtime

REEMBED_BATCH = 64
TRANSLATE_CONCURRENCY = 4


@dataclass
class ReembedReport:
    articles: int = 0
    translated: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"articles": self.articles, "translated": self.translated}


async def reembed_articles(runtime: Runtime, start: date, end: date) -> ReembedReport:
    """Embed again every embedded article with ``start <= edition_date <= end``."""
    report = ReembedReport()
    async with runtime.session_factory() as database:
        ids = list(
            (
                await database.scalars(
                    select(NewsroomArticle.id)
                    .where(
                        NewsroomArticle.edition_date.between(start, end),
                        NewsroomArticle.embedding.is_not(None),
                    )
                    .order_by(NewsroomArticle.edition_date, NewsroomArticle.first_seen_at)
                )
            ).all()
        )
    gate = asyncio.Semaphore(TRANSLATE_CONCURRENCY)
    for offset in range(0, len(ids), REEMBED_BATCH):
        batch = ids[offset : offset + REEMBED_BATCH]
        async with runtime.session_factory() as database:
            rows = (
                await database.execute(
                    select(
                        NewsroomArticle.id, NewsroomArticle.title, NewsroomArticle.feed_summary
                    ).where(NewsroomArticle.id.in_(batch))
                )
            ).all()

            async def text_for(article_id: uuid.UUID, title: str, summary: str | None) -> str:
                if not needs_translation(title):
                    return embedding_input(title, summary)
                async with gate:
                    return await translated_embedding_input(
                        runtime,
                        database,
                        article_id=article_id,
                        title=title,
                        feed_summary=summary,
                        last_attempt=True,
                    )

            texts = await asyncio.gather(*(text_for(*row) for row in rows))
            usable = [(row.id, text) for row, text in zip(rows, texts, strict=True) if text]
            vectors = await runtime.embedder.embed(
                database, [text for _, text in usable], audit=CallAudit("embed", None, "reembed")
            )
            for (article_id, text), vector in zip(usable, vectors, strict=True):
                await database.execute(
                    update(NewsroomArticle)
                    .where(NewsroomArticle.id == article_id)
                    .values(embedding=vector, embedding_text=text)
                )
            await database.commit()
        report.articles += len(usable)
        report.translated += sum(1 for row in rows if needs_translation(row.title))
    return report
