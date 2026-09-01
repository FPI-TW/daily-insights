import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.models import Base
from daily_insights_api.modules.news.contracts import (
    Candidate,
    LocalizedSummary,
    Selection,
)
from daily_insights_api.modules.news.llm import DeepSeekClient, ModelCall, ModelCallError
from daily_insights_api.modules.news.models import (
    NewsEdition,
    NewsGenerationAudit,
    NewsItem,
    NewsPresentation,
)
from daily_insights_api.modules.news.service import TAIPEI, run_news_edition
from daily_insights_api.modules.news.sources import FetchedCandidate

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def news_database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required for integration tests")
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


class _DeterministicNewsClient:
    model_name = "deepseek-chat"

    def __init__(self, prompt_marker: str) -> None:
        self.selection_prompt_digest = prompt_marker * 64
        self.selection_prompt_version = f"selection-v3:{prompt_marker * 12}"

    async def select(self, candidates: list[FetchedCandidate]) -> ModelCall:
        return ModelCall(
            Selection.model_validate(
                {
                    "selections": [
                        {
                            "id": candidates[0].candidate.id,
                            "topic": "markets",
                            "event_key": "global-markets",
                            "market": "global",
                            "importance": 5,
                        },
                        {
                            "id": candidates[1].candidate.id,
                            "topic": "companies",
                            "event_key": "company-results",
                            "market": "asia",
                            "importance": 3,
                        },
                    ]
                }
            ),
            "selection-request",
            10,
            5,
            1,
            self.selection_prompt_digest,
        )

    async def summarize(self, candidate: Candidate, article_text: str, locale: str) -> ModelCall:
        del article_text
        if candidate.id == "b" * 64 and locale == "en":
            raise ModelCallError(
                "terminal English summary failure",
                input_digest="e" * 64,
                latency_ms=1,
            )
        return ModelCall(
            LocalizedSummary(
                headline=f"{locale} headline",
                summary=f"{locale} summary",
            ),
            f"summary-{locale}",
            6,
            4,
            1,
            "c" * 64,
        )


def _fetched_candidates() -> list[FetchedCandidate]:
    return [
        FetchedCandidate(
            Candidate(
                id="a" * 64,
                url="https://www.reuters.com/markets",
                hostname="www.reuters.com",
                source_name="Reuters",
                headline="Markets move",
            ),
            "https://www.reuters.com/markets",
            "English article body",
            "1" * 64,
            datetime(2026, 9, 1, tzinfo=UTC),
        ),
        FetchedCandidate(
            Candidate(
                id="b" * 64,
                url="https://news.cnyes.com/company",
                hostname="news.cnyes.com",
                source_name="鉅亨",
                headline="企業公布業績",
            ),
            "https://news.cnyes.com/company",
            "繁體中文文章正文",
            "2" * 64,
            datetime(2026, 9, 1, tzinfo=UTC),
        ),
    ]


async def test_news_revisions_are_prompt_sensitive_and_published_items_have_all_locales(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = _fetched_candidates()

    async def discover(*args: object, **kwargs: object) -> list[Candidate]:
        del args, kwargs
        return [item.candidate for item in candidates]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return candidates

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_candidates", discover)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    edition_date = datetime.now(TAIPEI).date()
    first_client = cast(DeepSeekClient, _DeterministicNewsClient("a"))
    await run_news_edition(
        news_database,
        first_client,
        edition_date,
        allowed_hostnames="www.reuters.com,news.cnyes.com",
    )
    await run_news_edition(
        news_database,
        first_client,
        edition_date,
        allowed_hostnames="www.reuters.com,news.cnyes.com",
    )
    await run_news_edition(
        news_database,
        cast(DeepSeekClient, _DeterministicNewsClient("b")),
        edition_date,
        allowed_hostnames="www.reuters.com,news.cnyes.com",
    )

    async with news_database() as database:
        editions = list(await database.scalars(select(NewsEdition).order_by(NewsEdition.revision)))
        assert [edition.revision for edition in editions] == [1, 2]
        assert editions[0].input_digest != editions[1].input_digest
        assert editions[0].prompt_version.startswith("selection-v3:aaaaaaaaaaaa+")
        assert editions[1].prompt_version.startswith("selection-v3:bbbbbbbbbbbb+")
        for edition in editions:
            items = list(
                await database.scalars(select(NewsItem).where(NewsItem.edition_id == edition.id))
            )
            assert len(items) == 1
            assert items[0].source_url == "https://www.reuters.com/markets"
            presentations = list(
                await database.scalars(
                    select(NewsPresentation).where(NewsPresentation.item_id == items[0].id)
                )
            )
            assert {presentation.locale for presentation in presentations} == {
                "zh-hant",
                "zh-hans",
                "en",
            }
        assert (
            await database.scalar(
                select(func.count())
                .select_from(NewsItem)
                .where(NewsItem.source_url == "https://news.cnyes.com/company")
            )
            == 0
        )
        audit_versions = set(await database.scalars(select(NewsGenerationAudit.prompt_version)))
        assert "summary-v2" in audit_versions
        assert "selection-v3:aaaaaaaaaaaa" in audit_versions
        assert "selection-v3:bbbbbbbbbbbb" in audit_versions
