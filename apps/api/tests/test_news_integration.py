import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import ClassVar, cast

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
from daily_insights_api.modules.news.extraction import FetchedCandidate, configured_hostnames
from daily_insights_api.modules.news.llm import DeepSeekClient, ModelCall, ModelCallError
from daily_insights_api.modules.news.models import (
    NewsCandidate,
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
        self.selection_prompt_version = f"selection-v6:{prompt_marker * 12}"

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

    async def summarize(
        self,
        candidate: Candidate,
        article_text: str,
        locale: str,
        *,
        retry_feedback: str | None = None,
    ) -> ModelCall:
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

    async def feeds(*args: object, **kwargs: object) -> list[Candidate]:
        del args, kwargs
        return [item.candidate for item in candidates]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return candidates

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    edition_date = datetime.now(TAIPEI).date()
    first_client = cast(DeepSeekClient, _DeterministicNewsClient("a"))
    statuses = [
        await run_news_edition(
            news_database,
            first_client,
            edition_date,
            allowed_hostnames=configured_hostnames("www.reuters.com,news.cnyes.com"),
        ),
        await run_news_edition(
            news_database,
            first_client,
            edition_date,
            allowed_hostnames=configured_hostnames("www.reuters.com,news.cnyes.com"),
        ),
        await run_news_edition(
            news_database,
            cast(DeepSeekClient, _DeterministicNewsClient("b")),
            edition_date,
            allowed_hostnames=configured_hostnames("www.reuters.com,news.cnyes.com"),
        ),
    ]
    assert statuses == ["partial", "partial", "partial"]

    async with news_database() as database:
        editions = list(await database.scalars(select(NewsEdition).order_by(NewsEdition.revision)))
        assert [edition.revision for edition in editions] == [1, 2, 3]
        # A partial edition is not final: identical inputs produce a new revision.
        assert editions[0].input_digest == editions[1].input_digest
        assert editions[1].input_digest != editions[2].input_digest
        assert editions[0].prompt_version.startswith("selection-v6:aaaaaaaaaaaa+")
        assert editions[2].prompt_version.startswith("selection-v6:bbbbbbbbbbbb+")
        for edition in editions:
            items = list(
                await database.scalars(select(NewsItem).where(NewsItem.edition_id == edition.id))
            )
            assert len(items) == 1
            assert items[0].source_url == "https://www.reuters.com/markets"
            # Selection-stage classification is persisted for grouping and
            # cross-day event tracking.
            assert items[0].market is not None and items[0].event_key is not None
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
        assert "summary-v3" in audit_versions
        assert "selection-v6:aaaaaaaaaaaa" in audit_versions
        assert "selection-v6:bbbbbbbbbbbb" in audit_versions


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

    async def summarize(
        self,
        candidate: Candidate,
        article_text: str,
        locale: str,
        *,
        retry_feedback: str | None = None,
    ) -> ModelCall:
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
    allowed = configured_hostnames(
        "www.reuters.com,apnews.com,www.bbc.com,www.cnbc.com,news.cnyes.com"
    )

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
        allowed_hostnames=configured_hostnames("www.reuters.com,news.cnyes.com"),
    )
    assert status == "partial"
    assert sorted(candidate.id for candidate in fetched_inputs[0]) == ["a" * 64, "b" * 64]


async def test_market_edition_is_independent_from_the_global_digest(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from daily_insights_api.modules.news.editions import TW_EQUITY_SPEC
    from daily_insights_api.modules.news.service import run_all_editions

    candidates = _five_candidates()
    # The pool grows between runs so the Taiwan edition is first partial and
    # then, with fresh inputs, regenerated as complete.
    pool: list[FetchedCandidate] = candidates[:4]
    feed_markets: list[str] = []

    async def feeds(
        http: object,
        allowed: object,
        now: object = None,
        *,
        market: str = "global",
        bodies: dict[str, str] | None = None,
    ) -> list[Candidate]:
        del http, allowed, now, bodies
        feed_markets.append(market)
        return [item.candidate for item in pool]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return list(pool)

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    edition_date = datetime.now(TAIPEI).date()
    client = cast(DeepSeekClient, _CompleteNewsClient("a"))
    allowed = configured_hostnames(
        "www.reuters.com,apnews.com,www.bbc.com,www.cnbc.com,news.cnyes.com"
    )

    status = await run_news_edition(
        news_database, client, edition_date, allowed_hostnames=allowed, spec=TW_EQUITY_SPEC
    )
    # Four stories against a target of five is a partial market edition.
    assert status == "partial"
    assert feed_markets == ["tw_equity"]

    # The global digest still starts at revision 1 with its own idempotency.
    pool.append(candidates[4])
    assert (
        await run_news_edition(news_database, client, edition_date, allowed_hostnames=allowed)
        == "complete"
    )
    # Running every edition leaves the complete digest alone, regenerates the
    # partial Taiwan edition from its new inputs and produces the US edition.
    assert (
        await run_all_editions(news_database, client, edition_date, allowed_hostnames=allowed)
        == "idempotent"
    )

    async with news_database() as database:
        rows = list(
            await database.scalars(
                select(NewsEdition).order_by(NewsEdition.market_code, NewsEdition.revision)
            )
        )
        assert [(row.market_code, row.revision, row.status, row.caveat) for row in rows] == [
            ("global", 1, "complete", "5/5 stories completed"),
            ("tw_equity", 1, "partial", "4/5 stories completed"),
            ("tw_equity", 2, "complete", "5/5 stories completed"),
            ("us_equity", 1, "complete", "5/5 stories completed"),
        ]


async def test_only_missing_run_leaves_existing_editions_alone_and_fills_the_rest(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reclaimed automatic run must not regenerate a market it already produced."""
    from daily_insights_api.modules.news.service import run_all_editions

    candidates = _fetched_candidates()
    generated: list[str] = []

    async def feeds(
        http: object,
        allowed: object,
        now: object = None,
        *,
        market: str = "global",
        bodies: dict[str, str] | None = None,
    ) -> list[Candidate]:
        del http, allowed, now, bodies
        generated.append(market)
        return [item.candidate for item in candidates]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return candidates

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    edition_date = datetime.now(TAIPEI).date()
    client = cast(DeepSeekClient, _DeterministicNewsClient("a"))
    allowed = configured_hostnames("www.reuters.com,news.cnyes.com")
    # The first attempt produced a partial global edition and then died.
    assert (
        await run_news_edition(news_database, client, edition_date, allowed_hostnames=allowed)
        == "partial"
    )
    generated.clear()

    outcome = await run_all_editions(
        news_database, client, edition_date, allowed_hostnames=allowed, only_missing=True
    )

    # Global keeps its single partial revision; the two market editions are
    # generated now, and the worst existing status is reported.
    assert outcome == "partial"
    assert generated == ["tw_equity", "us_equity"]
    async with news_database() as database:
        rows = list(
            await database.scalars(
                select(NewsEdition).order_by(NewsEdition.market_code, NewsEdition.revision)
            )
        )
    assert [(row.market_code, row.revision) for row in rows] == [
        ("global", 1),
        ("tw_equity", 1),
        ("us_equity", 1),
    ]
    # A manual rerun (only_missing=False) still regenerates the partial edition.
    generated.clear()
    await run_all_editions(news_database, client, edition_date, allowed_hostnames=allowed)
    assert generated == ["global", "tw_equity", "us_equity"]


class _ImportanceNewsClient(_CompleteNewsClient):
    """Rates every story four stars except candidate 6, the only five-star one."""

    def __init__(self) -> None:
        super().__init__("a")
        self.batches: list[list[int]] = []

    async def select(self, candidates: list[FetchedCandidate], **kwargs: object) -> ModelCall:
        del kwargs
        indexes = [int(item.candidate.id[:1]) for item in candidates]
        self.batches.append(indexes)
        return ModelCall(
            Selection.model_validate(
                {
                    "selections": [
                        {
                            "id": item.candidate.id,
                            "topic": "markets" if index % 2 else "companies",
                            "event_key": f"story-{index}",
                            "market": "global",
                            "importance": 5 if index == 6 else 4,
                        }
                        for index, item in zip(indexes, candidates, strict=True)
                    ]
                }
            ),
            "selection-request",
            10,
            5,
            1,
            self.selection_prompt_digest,
        )


async def test_screening_rates_the_whole_pool_and_publishes_five_star_stories_first(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The five-star story sits in the second prompt window; it must still lead."""
    from dataclasses import replace

    from daily_insights_api.modules.news.editions import GLOBAL_SPEC

    hosts, candidates = _six_candidates()

    async def feeds(*args: object, **kwargs: object) -> list[Candidate]:
        del args, kwargs
        return [item.candidate for item in candidates]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return candidates

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    client = _ImportanceNewsClient()
    status = await run_news_edition(
        news_database,
        cast(DeepSeekClient, client),
        datetime.now(TAIPEI).date(),
        allowed_hostnames=frozenset(hosts),
        spec=replace(GLOBAL_SPEC, max_candidates=3),
    )
    assert status == "complete"
    # Both windows were rated before any story was summarised, even though
    # the first window alone held enough four-star stories.
    assert client.batches == [[1, 2, 3], [4, 5, 6]]
    async with news_database() as database:
        edition = (await database.scalars(select(NewsEdition))).one()
        items = list(
            await database.scalars(
                select(NewsItem).where(NewsItem.edition_id == edition.id).order_by(NewsItem.rank)
            )
        )
        rows = {
            int(row.candidate_id[:1]): row
            for row in await database.scalars(
                select(NewsCandidate).where(NewsCandidate.edition_id == edition.id)
            )
        }
    assert [item.source_hostname for item in items] == [
        "source6.example",
        "source1.example",
        "source2.example",
        "source3.example",
        "source4.example",
    ]
    assert [item.importance for item in items] == [5, 4, 4, 4, 4]
    assert (rows[5].stage, rows[5].drop_reason) == ("dropped", "reserve")


class _DuplicateEventNewsClient(_ImportanceNewsClient):
    """Candidates 1 and 4 report the same five-star event; candidate 1 cannot be summarised."""

    async def select(self, candidates: list[FetchedCandidate], **kwargs: object) -> ModelCall:
        del kwargs
        indexes = [int(item.candidate.id[:1]) for item in candidates]
        self.batches.append(indexes)
        return ModelCall(
            Selection.model_validate(
                {
                    "selections": [
                        {
                            "id": item.candidate.id,
                            "topic": "markets" if index % 2 else "companies",
                            "event_key": "story-1" if index in {1, 4} else f"story-{index}",
                            "market": "global",
                            "importance": 5 if index in {1, 4} else 4,
                        }
                        for index, item in zip(indexes, candidates, strict=True)
                    ]
                }
            ),
            "selection-request",
            10,
            5,
            1,
            self.selection_prompt_digest,
        )

    async def summarize(
        self,
        candidate: Candidate,
        article_text: str,
        locale: str,
        *,
        retry_feedback: str | None = None,
    ) -> ModelCall:
        if candidate.id[:1] == "1":
            raise ModelCallError(
                "unsupported number",
                input_digest="f" * 64,
                latency_ms=1,
                error_code="summary_ungrounded_number",
            )
        return await super().summarize(
            candidate, article_text, locale, retry_feedback=retry_feedback
        )


def _six_candidates() -> tuple[list[str], list[FetchedCandidate]]:
    hosts = [f"source{index}.example" for index in range(1, 7)]
    return hosts, [
        FetchedCandidate(
            Candidate(
                id=str(index) * 64,
                url=f"https://{host}/story-{index}",
                hostname=host,
                source_name=host,
                headline=f"Story {index}",
                seen_at=datetime(2026, 9, 8, tzinfo=UTC),
            ),
            f"https://{host}/story-{index}",
            f"Body {index}",
            str(index) * 64,
            datetime(2026, 9, 8, tzinfo=UTC),
        )
        for index, host in enumerate(hosts, start=1)
    ]


async def test_second_window_report_of_an_event_replaces_a_first_window_summary_failure(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from daily_insights_api.modules.news.editions import GLOBAL_SPEC

    hosts, candidates = _six_candidates()

    async def feeds(*args: object, **kwargs: object) -> list[Candidate]:
        del args, kwargs
        return [item.candidate for item in candidates]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return candidates

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    status = await run_news_edition(
        news_database,
        cast(DeepSeekClient, _DuplicateEventNewsClient()),
        datetime.now(TAIPEI).date(),
        allowed_hostnames=frozenset(hosts),
        spec=replace(GLOBAL_SPEC, max_candidates=3),
    )
    assert status == "complete"
    async with news_database() as database:
        edition = (await database.scalars(select(NewsEdition))).one()
        items = list(
            await database.scalars(
                select(NewsItem).where(NewsItem.edition_id == edition.id).order_by(NewsItem.rank)
            )
        )
        rows = {
            int(row.candidate_id[:1]): row
            for row in await database.scalars(
                select(NewsCandidate).where(NewsCandidate.edition_id == edition.id)
            )
        }
    # The five-star event still leads through its second-window report.
    assert [item.source_hostname for item in items][:1] == ["source4.example"]
    assert items[0].event_key == "story-1" and items[0].importance == 5
    assert (rows[1].stage, rows[1].drop_reason) == ("dropped", "summary_failed")
    assert rows[4].stage == "published"


class _SameDomainNewsClient(_ImportanceNewsClient):
    """Every window returns five-star stories from one domain plus four-star fillers."""

    def __init__(self) -> None:
        super().__init__()
        self.summarized: list[str] = []

    async def select(self, candidates: list[FetchedCandidate], **kwargs: object) -> ModelCall:
        del kwargs
        indexes = [int(item.candidate.id[:1]) for item in candidates]
        self.batches.append(indexes)
        return ModelCall(
            Selection.model_validate(
                {
                    "selections": [
                        {
                            "id": item.candidate.id,
                            "topic": "markets" if index % 2 else "companies",
                            "event_key": f"story-{index}",
                            "market": "global",
                            "importance": 5 if item.candidate.hostname == "wire.example" else 4,
                        }
                        for index, item in zip(indexes, candidates, strict=True)
                    ]
                }
            ),
            "selection-request",
            10,
            5,
            1,
            self.selection_prompt_digest,
        )

    async def summarize(
        self,
        candidate: Candidate,
        article_text: str,
        locale: str,
        *,
        retry_feedback: str | None = None,
    ) -> ModelCall:
        self.summarized.append(candidate.id[:1])
        return await super().summarize(
            candidate, article_text, locale, retry_feedback=retry_feedback
        )


async def test_domain_quota_is_settled_before_summaries_are_paid_for(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three five-star wire stories across two windows; the global cap is two per domain."""
    from dataclasses import replace

    from daily_insights_api.modules.news.editions import GLOBAL_SPEC

    hosts, candidates = _six_candidates()
    # Candidates 1, 2 (window one) and 4 (window two) come from the same wire.
    wire = {1, 2, 4}
    candidates = [
        FetchedCandidate(
            Candidate(
                id=item.candidate.id,
                url=item.candidate.url,
                hostname="wire.example"
                if int(item.candidate.id[:1]) in wire
                else item.candidate.hostname,
                source_name=item.candidate.source_name,
                headline=item.candidate.headline,
                seen_at=item.candidate.seen_at,
            ),
            item.source_url,
            item.body,
            item.content_digest,
            item.source_published_at,
        )
        for item in candidates
    ]

    async def feeds(*args: object, **kwargs: object) -> list[Candidate]:
        del args, kwargs
        return [item.candidate for item in candidates]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return candidates

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    client = _SameDomainNewsClient()
    status = await run_news_edition(
        news_database,
        cast(DeepSeekClient, client),
        datetime.now(TAIPEI).date(),
        allowed_hostnames=frozenset([*hosts, "wire.example"]),
        spec=replace(GLOBAL_SPEC, max_candidates=3),
    )
    assert status == "complete"
    # Candidate 4 is the third wire story: skipped without a single summary call.
    assert "4" not in client.summarized
    assert len(client.summarized) == 15
    async with news_database() as database:
        edition = (await database.scalars(select(NewsEdition))).one()
        items = list(
            await database.scalars(
                select(NewsItem).where(NewsItem.edition_id == edition.id).order_by(NewsItem.rank)
            )
        )
        rows = {
            int(row.candidate_id[:1]): row
            for row in await database.scalars(
                select(NewsCandidate).where(NewsCandidate.edition_id == edition.id)
            )
        }
    assert [item.importance for item in items] == [5, 5, 4, 4, 4]
    assert (rows[4].stage, rows[4].drop_reason) == ("dropped", "policy")


async def test_thin_discovery_reports_the_candidate_floor(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from daily_insights_api.modules.news import service as news_service

    candidates = _fetched_candidates()
    events: list[tuple[str, dict[str, object]]] = []

    async def feeds(*args: object, **kwargs: object) -> list[Candidate]:
        del args, kwargs
        return [item.candidate for item in candidates]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return candidates

    monkeypatch.setattr(news_service, "discover_feed_candidates", feeds)
    monkeypatch.setattr(news_service, "_fetch_usable_candidates", fetch)
    monkeypatch.setattr(
        news_service, "emit_event", lambda name, **fields: events.append((name, fields))
    )
    status = await run_news_edition(
        news_database,
        cast(DeepSeekClient, _DeterministicNewsClient("a")),
        datetime.now(TAIPEI).date(),
        allowed_hostnames=configured_hostnames("www.reuters.com,news.cnyes.com"),
    )
    # Two candidates against a floor of ten (five stories, doubled) still run,
    # but the shortfall is reported before the model is called.
    assert status == "partial"
    floor = [fields for name, fields in events if name == "news.candidates.below_floor"]
    assert floor == [{"market": "global", "count": 2, "floor": 10}]


@pytest.mark.parametrize("market_code", ["global", "tw_equity", "us_equity"])
@pytest.mark.parametrize("age_days", [0, 1, 7])
@pytest.mark.parametrize("locale", ["zh-hant", "zh-hans", "en"])
async def test_latest_news_preserves_last_publishable_edition(
    news_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    age_days: int,
    locale: str,
    market_code: str,
) -> None:
    from datetime import timedelta

    from fastapi import Response

    from daily_insights_api.modules.news.contracts import Locale
    from daily_insights_api.modules.news.editions import edition_spec
    from daily_insights_api.modules.news.router import _latest_response

    candidates = _fetched_candidates()

    async def feeds(*args: object, **kwargs: object) -> list[Candidate]:
        return [item.candidate for item in candidates]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        return candidates

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    today = datetime.now(TAIPEI).date()
    await run_news_edition(
        news_database,
        cast(DeepSeekClient, _DeterministicNewsClient("a")),
        today,
        allowed_hostnames=configured_hostnames("www.reuters.com,news.cnyes.com"),
    )
    async with news_database() as database:
        good = (await database.scalars(select(NewsEdition))).one()
        good.market_code = market_code
        good.edition_date = today - timedelta(days=age_days)
        good_id = good.id
        # Newest attempt fails; an empty partial and a future edition must not
        # displace the last actually published, localized stories either.
        for revision, status, day in [
            (2, "unavailable", today),
            (3, "partial", today),
            (4, "complete", today + timedelta(days=1)),
        ]:
            database.add(
                NewsEdition(
                    edition_date=day,
                    market_code=market_code,
                    revision=revision,
                    input_digest="f" * 64,
                    derivation_version="test",
                    prompt_version="test",
                    status=status,
                )
            )
        await database.flush()
        future = (
            await database.scalars(select(NewsEdition).where(NewsEdition.edition_date > today))
        ).one()
        original = (
            await database.scalars(select(NewsItem).where(NewsItem.edition_id == good_id))
        ).one()
        future_item = NewsItem(
            **{
                column.name: getattr(original, column.name)
                for column in NewsItem.__table__.columns
                if column.name not in {"id", "edition_id"}
            },
            edition_id=future.id,
        )
        database.add(future_item)
        await database.flush()
        for language in ("zh-hant", "zh-hans", "en"):
            database.add(
                NewsPresentation(
                    item_id=future_item.id,
                    locale=language,
                    headline="Future news",
                    summary="Future news",
                )
            )
        await database.commit()
        result = await _latest_response(
            database, Response(), cast(Locale, locale), edition_spec(market_code)
        )
        assert result.edition_id == good_id
        assert result.items
        assert result.edition_date == today - timedelta(days=age_days)
        # Falling back to an older edition is silent: no date notice is shown.
        assert result.caveat is None


class _RefillNewsClient(_CompleteNewsClient):
    def __init__(self, *, fail_refill: bool = False, duplicate_event: bool = False) -> None:
        super().__init__("a")
        self.batches: list[list[str]] = []
        self.histories: list[object] = []
        self.summarized: list[str] = []
        self.fail_refill = fail_refill
        self.duplicate_event = duplicate_event

    async def select(self, candidates: list[FetchedCandidate], **kwargs: object) -> ModelCall:
        self.batches.append([item.candidate.id for item in candidates])
        self.histories.append(kwargs.get("previous_events"))
        if self.fail_refill and len(self.batches) > 1:
            raise ModelCallError("temporary outage", input_digest="f" * 64, latency_ms=1)
        chosen = []
        for item in candidates[:3]:
            index = int(item.candidate.id, 16)
            chosen.append(
                {
                    "id": item.candidate.id,
                    "topic": "markets" if index % 2 else "companies",
                    "event_key": "event-2"
                    if self.duplicate_event and index == 4
                    else f"event-{index}",
                    "market": "global",
                    "importance": 4,
                }
            )
        return ModelCall(
            Selection.model_validate({"selections": chosen}), "select", 10, 5, 1, "a" * 64
        )

    async def summarize(
        self,
        candidate: Candidate,
        article_text: str,
        locale: str,
        *,
        retry_feedback: str | None = None,
    ) -> ModelCall:
        self.summarized.append(candidate.id)
        if int(candidate.id, 16) == 1:
            raise ModelCallError(
                "unsupported number",
                input_digest="f" * 64,
                latency_ms=1,
                error_code="summary_ungrounded_number",
            )
        return await super().summarize(
            candidate, article_text, locale, retry_feedback=retry_feedback
        )


@pytest.mark.parametrize("market_code", ["global", "tw_equity", "us_equity"])
async def test_refill_reaches_target_after_initial_summary_failure(
    news_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    market_code: str,
) -> None:
    from daily_insights_api.modules.news.editions import edition_spec

    await _assert_refill(
        news_database,
        monkeypatch,
        _RefillNewsClient(),
        market_code,
        edition_spec(market_code).target_items,
    )


async def test_refill_deduplicates_events_across_rounds_before_summarizing(
    news_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RefillNewsClient(duplicate_event=True)
    await _assert_refill(news_database, monkeypatch, client, "global", 5)
    assert len(client.batches) == 3
    assert f"{4:064x}" in client.batches[1]
    assert f"{4:064x}" not in client.summarized


async def test_refill_outage_preserves_successful_stories_and_audits_failure(
    news_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _assert_refill(
        news_database, monkeypatch, _RefillNewsClient(fail_refill=True), "global", 2
    )
    async with news_database() as database:
        failed = list(
            await database.scalars(
                select(NewsGenerationAudit).where(
                    NewsGenerationAudit.stage == "selection", NewsGenerationAudit.status == "failed"
                )
            )
        )
        assert len(failed) == 2


async def _assert_refill(
    database_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    client: _RefillNewsClient,
    market_code: str,
    expected: int,
    *,
    max_candidates: int | None = None,
) -> None:
    from dataclasses import replace

    from daily_insights_api.modules.news.editions import edition_spec
    from daily_insights_api.modules.news.llm import enforce_selection_policy

    candidates = [
        FetchedCandidate(
            Candidate(
                id=f"{i:064x}",
                url=f"https://source{i:02d}.example/story",
                hostname=f"source{i:02d}.example",
                source_name=f"Source {i}",
                headline=f"Event {i}",
            ),
            f"https://source{i:02d}.example/story",
            "Source body",
            f"{i:064x}",
        )
        for i in range(1, 13)
    ]

    async def feeds(*args: object, **kwargs: object) -> list[Candidate]:
        return [item.candidate for item in candidates]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        return candidates

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    spec = edition_spec(market_code)
    if max_candidates is not None:
        spec = replace(spec, max_candidates=max_candidates)
    result = await run_news_edition(
        database_factory,
        cast(DeepSeekClient, client),
        datetime.now(TAIPEI).date(),
        allowed_hostnames=frozenset(f.candidate.hostname for f in candidates),
        spec=spec,
    )
    assert result == ("complete" if expected == spec.target_items else "partial")
    assert 2 <= len(client.batches) <= 3
    assert f"{1:064x}" not in client.batches[1]
    assert client.histories[1]
    async with database_factory() as database:
        items = list(await database.scalars(select(NewsItem).order_by(NewsItem.rank)))
        assert len(items) == expected
        assert [item.rank for item in items] == list(range(1, expected + 1))
        assert len({item.event_key for item in items}) == expected
        assert (
            await database.scalar(select(func.count()).select_from(NewsPresentation))
            == expected * 3
        )
        selected = Selection.model_validate(
            {
                "selections": [
                    {
                        "id": candidates[int(item.source_name.split()[1]) - 1].candidate.id,
                        "topic": item.topic,
                        "event_key": item.event_key,
                        "market": item.market,
                        "importance": item.importance,
                    }
                    for item in items
                ]
            }
        )
        enforce_selection_policy(selected, candidates, spec.selection)


async def test_refill_stops_after_three_rounds_when_target_is_still_short(
    news_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OneAtATimeClient(_RefillNewsClient):
        async def select(self, candidates: list[FetchedCandidate], **kwargs: object) -> ModelCall:
            return await super().select(candidates[:1], **kwargs)

        async def summarize(
            self,
            candidate: Candidate,
            article_text: str,
            locale: str,
            *,
            retry_feedback: str | None = None,
        ) -> ModelCall:
            return await _CompleteNewsClient.summarize(
                self, candidate, article_text, locale, retry_feedback=retry_feedback
            )

    client = OneAtATimeClient()
    await _assert_refill(news_database, monkeypatch, client, "global", 3)
    assert len(client.batches) == 3


async def test_refill_uses_candidates_beyond_the_first_prompt_window(
    news_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RefillNewsClient()
    await _assert_refill(news_database, monkeypatch, client, "global", 5, max_candidates=3)
    assert len(client.batches) == 2
    assert set(client.batches[0]).isdisjoint(client.batches[1])


class _StageNewsClient(_CompleteNewsClient):
    """Answers with a fixed table so every candidate stage is reachable."""

    MARKETS: ClassVar[dict[int, str]] = {1: "global", 2: "global", 3: "asia", 4: "global"}

    async def select(self, candidates: list[FetchedCandidate], **kwargs: object) -> ModelCall:
        from daily_insights_api.modules.news.editions import SelectionPolicy
        from daily_insights_api.modules.news.llm import filter_selection_markets

        present = {int(item.candidate.id[:1]) for item in candidates}
        original = Selection.model_validate(
            {
                "selections": [
                    {
                        "id": str(index) * 64,
                        "topic": "markets" if index % 2 else "companies",
                        "event_key": f"story-{index}",
                        "market": market,
                        "importance": 4,
                    }
                    for index, market in self.MARKETS.items()
                    if index in present
                ]
            }
        )
        # Mirror the real client: the edition's market filter runs inside
        # select and the model's own answer is reported alongside.
        kept, dropped = filter_selection_markets(original, cast(SelectionPolicy, kwargs["policy"]))
        return ModelCall(
            kept,
            "selection-request",
            10,
            5,
            1,
            self.selection_prompt_digest,
            rejected=tuple((item, "off_market") for item in dropped),
            returned=original.selections,
        )

    async def summarize(
        self,
        candidate: Candidate,
        article_text: str,
        locale: str,
        *,
        retry_feedback: str | None = None,
    ) -> ModelCall:
        if candidate.id.startswith("4") and locale == "en":
            raise ModelCallError(
                "ungrounded number",
                input_digest="e" * 64,
                latency_ms=1,
                error_code="summary_ungrounded_number",
            )
        return await super().summarize(
            candidate, article_text, locale, retry_feedback=retry_feedback
        )


async def test_edition_records_every_candidate_with_the_stage_it_reached(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from daily_insights_api.modules.news.editions import GLOBAL_SPEC

    hosts = [f"source{index}.example" for index in range(1, 8)]
    fetched = [
        FetchedCandidate(
            Candidate(
                id=str(index) * 64,
                url=f"https://{host}/story-{index}",
                hostname=host,
                source_name=host,
                headline=f"Story {index}",
                # Candidate 7 has no timestamp and is the first one cut by the
                # discovery budget.
                seen_at=datetime(2026, 9, 8, 0, index, tzinfo=UTC) if index < 7 else None,
            ),
            f"https://{host}/story-{index}",
            f"Body {index}",
            str(index) * 64,
            datetime(2026, 9, 8, tzinfo=UTC),
        )
        for index, host in enumerate(hosts, start=1)
    ]

    async def feeds(*args: object, **kwargs: object) -> list[Candidate]:
        del args, kwargs
        return [item.candidate for item in fetched]

    async def fetch(
        discovered: list[Candidate], *args: object, **kwargs: object
    ) -> list[FetchedCandidate]:
        del args, kwargs
        assert sorted(candidate.id[:1] for candidate in discovered) == list("123456")
        # Candidate 6 yields no usable text.
        return [item for item in fetched if item.candidate.id[:1] in {"1", "2", "3", "4", "5"}]

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    status = await run_news_edition(
        news_database,
        cast(DeepSeekClient, _StageNewsClient("a")),
        datetime.now(TAIPEI).date(),
        allowed_hostnames=frozenset(hosts),
        spec=replace(GLOBAL_SPEC, max_discovery_total=6),
    )
    assert status == "partial"
    async with news_database() as database:
        edition = (await database.scalars(select(NewsEdition))).one()
        items = {
            item.source_hostname: item
            for item in await database.scalars(
                select(NewsItem).where(NewsItem.edition_id == edition.id)
            )
        }
        rows = {
            int(row.candidate_id[:1]): row
            for row in await database.scalars(
                select(NewsCandidate).where(NewsCandidate.edition_id == edition.id)
            )
        }
    assert {(index, row.stage, row.drop_reason) for index, row in rows.items()} == {
        (1, "published", None),
        (2, "published", None),
        (3, "dropped", "off_market"),
        (4, "dropped", "summary_failed"),
        (5, "reviewed", None),
        (6, "fetch_failed", None),
        (7, "discovered", None),
    }
    assert rows[1].item_id == items["source1.example"].id
    assert rows[2].item_id == items["source2.example"].id
    assert [rows[index].ai_rank for index in (1, 2, 3, 4, 5)] == [1, 2, 3, 4, None]
    assert (rows[3].ai_market, rows[3].ai_topic, rows[3].ai_importance) == ("asia", "markets", 4)
    assert rows[3].ai_event_key == "story-3"
    assert rows[5].content_digest == "5" * 64 and rows[6].content_digest is None
    assert rows[7].seen_at is None and rows[1].seen_at is not None
    assert all(row.item_id is None for index, row in rows.items() if index > 2)


async def test_unavailable_edition_still_records_what_was_discovered(
    news_database: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = _fetched_candidates()

    async def feeds(*args: object, **kwargs: object) -> list[Candidate]:
        del args, kwargs
        return [item.candidate for item in candidates]

    async def fetch(*args: object, **kwargs: object) -> list[FetchedCandidate]:
        del args, kwargs
        return []

    monkeypatch.setattr("daily_insights_api.modules.news.service.discover_feed_candidates", feeds)
    monkeypatch.setattr("daily_insights_api.modules.news.service._fetch_usable_candidates", fetch)
    status = await run_news_edition(
        news_database,
        cast(DeepSeekClient, _DeterministicNewsClient("a")),
        datetime.now(TAIPEI).date(),
        allowed_hostnames=configured_hostnames("www.reuters.com,news.cnyes.com"),
    )
    assert status == "unavailable"
    async with news_database() as database:
        stages = list(await database.scalars(select(NewsCandidate.stage)))
    assert stages == ["fetch_failed", "fetch_failed"]
