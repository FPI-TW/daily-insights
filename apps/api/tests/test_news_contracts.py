from datetime import datetime, timedelta
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.modules.news.contracts import Candidate, LocalizedSummary, Selection
from daily_insights_api.modules.news.llm import DeepSeekClient
from daily_insights_api.modules.news.service import run_news_edition
from daily_insights_api.modules.news.sources import (
    allowed_hostname,
    configured_hostnames,
    validate_https_url,
)

ALLOWED = configured_hostnames(
    "www.reuters.com,apnews.com,www.bbc.com,www.cnbc.com,news.cnyes.com,finance.eastmoney.com"
)


def test_only_exact_allowlisted_hostname_is_allowed() -> None:
    assert allowed_hostname("www.reuters.com", ALLOWED)
    assert not allowed_hostname("reuters.com", ALLOWED)
    assert not allowed_hostname("reuters.com.evil.test", ALLOWED)


def test_source_url_rejects_private_scheme_credentials_and_unapproved_host() -> None:
    with pytest.raises(ValueError):
        validate_https_url("http://www.reuters.com/a", ALLOWED)
    with pytest.raises(ValueError):
        validate_https_url("https://user:password@www.reuters.com/a", ALLOWED)
    with pytest.raises(ValueError):
        validate_https_url("https://127.0.0.1/a", ALLOWED)


def test_selection_rejects_duplicate_ids() -> None:
    candidate_id = "a" * 64
    with pytest.raises(ValueError, match="unique"):
        Selection.model_validate(
            {
                "selections": [
                    {
                        "id": candidate_id,
                        "topic": "markets",
                        "event_key": "market-move",
                        "market": "global",
                        "importance": 5,
                    }
                ]
                * 2
            }
        )


def test_selection_requires_topic_and_market_diversity_for_three_or_more_items() -> None:
    selection = [
        {
            "id": character * 64,
            "topic": "markets",
            "event_key": f"event-{index}",
            "market": "global",
            "importance": 4,
        }
        for index, character in enumerate(("a", "b", "c"), start=1)
    ]
    with pytest.raises(ValueError, match="two topics"):
        Selection.model_validate({"selections": selection})

    diverse_topics = [
        {**selection[1], "topic": "economy"},
        {**selection[2], "topic": "companies"},
    ]
    with pytest.raises(ValueError, match="two markets"):
        Selection.model_validate({"selections": [selection[0], *diverse_topics]})

    valid = Selection.model_validate(
        {
            "selections": [
                selection[0],
                {**selection[1], "topic": "economy", "market": "us"},
                {**selection[2], "topic": "companies", "market": "asia"},
            ]
        }
    )
    assert len(valid.selections) == 3


def test_contracts_do_not_have_article_body_fields() -> None:
    candidate = Candidate(
        id="a" * 64,
        url="https://www.reuters.com/example",
        hostname="www.reuters.com",
        source_name="Reuters",
        headline="A headline",
    )
    assert "body" not in candidate.model_dump()
    assert LocalizedSummary(headline="Title", summary="Summary").numeric_facts == ()


async def test_news_runner_refuses_to_backfill_yesterday() -> None:
    with pytest.raises(ValueError, match="current Taipei"):
        await run_news_edition(
            cast(async_sessionmaker[AsyncSession], None),
            cast(DeepSeekClient, None),
            datetime.now().date() - timedelta(days=1),
            allowed_hostnames=",".join(ALLOWED),
        )


async def test_news_runner_refuses_future_edition() -> None:
    with pytest.raises(ValueError, match="current Taipei"):
        await run_news_edition(
            cast(async_sessionmaker[AsyncSession], None),
            cast(DeepSeekClient, None),
            datetime.now().date() + timedelta(days=1),
            allowed_hostnames=",".join(ALLOWED),
        )
