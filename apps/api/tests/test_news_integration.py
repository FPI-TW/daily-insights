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
from daily_insights_api.modules.news.extraction import FetchedCandidate
from daily_insights_api.modules.news.llm import DeepSeekClient, ModelCall, ModelCallError
from daily_insights_api.modules.news.models import (
    NewsEdition,
    NewsGenerationAudit,
    NewsItem,
    NewsPresentation,
)
from daily_insights_api.modules.news.service import TAIPEI, run_news_edition

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

    async def select(self, candidates: list[FetchedCandidate], **kwargs: object) -> ModelCall:
        del kwargs
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


async def test_partial_editions_regenerate_and_revisions_are_prompt_sensitive(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = _fetched_candidates()

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return candidates

    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    edition_date = datetime.now(TAIPEI).date()
    first_client = cast(DeepSeekClient, _DeterministicNewsClient("a"))
    statuses = [
        await run_news_edition(
            news_database,
            first_client,
            edition_date,
            allowed_hostnames="www.reuters.com,news.cnyes.com",
        ),
        await run_news_edition(
            news_database,
            first_client,
            edition_date,
            allowed_hostnames="www.reuters.com,news.cnyes.com",
        ),
        await run_news_edition(
            news_database,
            cast(DeepSeekClient, _DeterministicNewsClient("b")),
            edition_date,
            allowed_hostnames="www.reuters.com,news.cnyes.com",
        ),
    ]
    assert statuses == ["partial", "partial", "partial"]

    async with news_database() as database:
        editions = list(await database.scalars(select(NewsEdition).order_by(NewsEdition.revision)))
        assert [edition.revision for edition in editions] == [1, 2, 3]
        # A partial edition is not final: identical inputs produce a new revision.
        assert editions[0].input_digest == editions[1].input_digest
        assert editions[1].input_digest != editions[2].input_digest
        assert editions[0].prompt_version.startswith("selection-v3:aaaaaaaaaaaa+")
        assert editions[2].prompt_version.startswith("selection-v3:bbbbbbbbbbbb+")
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


class _CompleteNewsClient(_DeterministicNewsClient):
    async def select(self, candidates: list[FetchedCandidate], **kwargs: object) -> ModelCall:
        del kwargs
        return ModelCall(
            Selection.model_validate(
                {
                    "selections": [
                        {
                            "id": fetched.candidate.id,
                            "topic": "markets" if index % 2 else "companies",
                            "event_key": f"story-{index}",
                            "market": "global" if index % 2 else "asia",
                            "importance": 3,
                        }
                        for index, fetched in enumerate(candidates[:5])
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
        return ModelCall(
            LocalizedSummary(headline=f"{locale} {candidate.id[:4]}", summary=f"{locale} summary"),
            f"summary-{locale}",
            6,
            4,
            1,
            "c" * 64,
        )


def _five_candidates() -> list[FetchedCandidate]:
    hosts = ("www.reuters.com", "apnews.com", "www.bbc.com", "www.cnbc.com", "news.cnyes.com")
    return [
        FetchedCandidate(
            Candidate(
                id=str(index) * 64,
                url=f"https://{host}/story-{index}",
                hostname=host,
                source_name=host,
                headline=f"Story {index}",
            ),
            f"https://{host}/story-{index}",
            f"Body {index}",
            str(index) * 64,
            datetime(2026, 9, 1, tzinfo=UTC),
        )
        for index, host in enumerate(hosts, start=1)
    ]


async def test_complete_edition_is_idempotent_but_unavailable_edition_regenerates(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched: list[FetchedCandidate] = []

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return list(fetched)

    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    edition_date = datetime.now(TAIPEI).date()
    client = cast(DeepSeekClient, _CompleteNewsClient("a"))
    allowed = "www.reuters.com,apnews.com,www.bbc.com,www.cnbc.com,news.cnyes.com"

    # No usable candidates: the edition is unavailable and may be retried later.
    assert await run_news_edition(
        news_database, client, edition_date, allowed_hostnames=allowed
    ) == ("unavailable")
    assert await run_news_edition(
        news_database, client, edition_date, allowed_hostnames=allowed
    ) == ("unavailable")

    # Candidates appear: a complete edition is produced and then held stable.
    fetched.extend(_five_candidates())
    assert await run_news_edition(
        news_database, client, edition_date, allowed_hostnames=allowed
    ) == ("complete")
    assert await run_news_edition(
        news_database, client, edition_date, allowed_hostnames=allowed
    ) == ("idempotent")

    async with news_database() as database:
        editions = list(await database.scalars(select(NewsEdition).order_by(NewsEdition.revision)))
        assert [(edition.revision, edition.status) for edition in editions] == [
            (1, "unavailable"),
            (2, "unavailable"),
            (3, "complete"),
        ]
        assert editions[0].input_digest == editions[1].input_digest
        assert (
            await database.scalar(
                select(func.count())
                .select_from(NewsItem)
                .where(NewsItem.edition_id == editions[2].id)
            )
            == 5
        )


async def test_feed_discovery_supplies_the_candidates(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = _fetched_candidates()

    async def feeds(
        http: object, allowed: object, now: object = None, **kwargs: object
    ) -> list[Candidate]:
        del http, allowed, now, kwargs
        return [item.candidate for item in candidates]

    fetched_inputs: list[list[Candidate]] = []

    async def fetch(
        discovered: list[Candidate], *args: object, **kwargs: object
    ) -> list[FetchedCandidate]:
        del args, kwargs
        fetched_inputs.append(discovered)
        return candidates

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)

    status = await run_news_edition(
        news_database,
        cast(DeepSeekClient, _DeterministicNewsClient("a")),
        datetime.now(TAIPEI).date(),
        allowed_hostnames="www.reuters.com,news.cnyes.com",
    )
    assert status == "partial"
    assert sorted(candidate.id for candidate in fetched_inputs[0]) == ["a" * 64, "b" * 64]


async def test_market_edition_is_independent_from_the_global_digest(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from daily_insights_api.modules.news.editions import TW_EQUITY_SPEC
    from daily_insights_api.modules.news.service import run_all_editions

    candidates = _five_candidates()
    feed_markets: list[str] = []

    async def feeds(
        http: object, allowed: object, now: object = None, *, market: str = "global"
    ) -> list[Candidate]:
        del http, allowed, now
        feed_markets.append(market)
        return [item.candidate for item in candidates]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return candidates

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    edition_date = datetime.now(TAIPEI).date()
    client = cast(DeepSeekClient, _CompleteNewsClient("a"))
    allowed = "www.reuters.com,apnews.com,www.bbc.com,www.cnbc.com,news.cnyes.com"

    status = await run_news_edition(
        news_database, client, edition_date, allowed_hostnames=allowed, spec=TW_EQUITY_SPEC
    )
    # Five stories against a target of eight is a partial market edition.
    assert status == "partial"
    assert feed_markets == ["tw_equity"]

    # The global digest still starts at revision 1 with its own idempotency.
    assert (
        await run_news_edition(news_database, client, edition_date, allowed_hostnames=allowed)
        == "complete"
    )
    assert (
        await run_all_editions(news_database, client, edition_date, allowed_hostnames=allowed)
        == "partial"
    )

    async with news_database() as database:
        rows = list(
            await database.scalars(
                select(NewsEdition).order_by(NewsEdition.market_code, NewsEdition.revision)
            )
        )
        assert [(row.market_code, row.revision, row.status, row.caveat) for row in rows] == [
            ("global", 1, "complete", "5/5 stories completed"),
            ("tw_equity", 1, "partial", "5/8 stories completed"),
            ("tw_equity", 2, "partial", "5/8 stories completed"),
            ("us_equity", 1, "partial", "5/8 stories completed"),
        ]
