from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.modules.news.contracts import Candidate, LocalizedSummary, Selection
from daily_insights_api.modules.news.extraction import (
    allowed_hostname,
    configured_hostnames,
    validate_https_url,
)
from daily_insights_api.modules.news.llm import DeepSeekClient
from daily_insights_api.modules.news.service import run_news_edition

ALLOWED = configured_hostnames(
    "www.reuters.com,apnews.com,www.bbc.com,www.cnbc.com,news.cnyes.com,finance.eastmoney.com"
)
TAIPEI = ZoneInfo("Asia/Taipei")


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


def test_selection_contract_is_structural_and_allows_market_editions_up_to_ten() -> None:
    selection = [
        {
            "id": character * 64,
            "topic": "markets",
            "event_key": f"event-{index}",
            "market": "taiwan",
            "importance": 4,
        }
        for index, character in enumerate("abcdef01", start=1)
    ]
    # Diversity and per-domain limits are edition policy, not contract shape.
    assert len(Selection.model_validate({"selections": selection}).selections) == 8
    with pytest.raises(ValueError, match="unique event keys"):
        Selection.model_validate(
            {"selections": [selection[0], {**selection[1], "event_key": "event-1"}]}
        )
    with pytest.raises(ValueError):
        Selection.model_validate({"selections": selection + selection[:3]})


def test_selection_allows_one_distinct_reserve_for_the_same_event() -> None:
    selection = Selection.model_validate(
        {
            "selections": [
                {
                    "id": "a" * 64,
                    "topic": "policy",
                    "event_key": "fed-rate-decision",
                    "market": "global",
                    "importance": 5,
                }
            ],
            "reserves": [
                {
                    "id": "b" * 64,
                    "topic": "policy",
                    "event_key": "fed-rate-decision",
                    "market": "global",
                    "importance": 5,
                }
            ],
        }
    )

    assert selection.reserves[0].event_key == selection.selections[0].event_key
    with pytest.raises(ValueError, match="reserve candidates must have unique event keys"):
        duplicate_reserve = selection.reserves[0].model_copy(update={"id": "c" * 64})
        Selection.model_validate(
            {
                "selections": selection.model_dump(mode="json")["selections"],
                "reserves": [
                    *selection.model_dump(mode="json")["reserves"],
                    duplicate_reserve.model_dump(mode="json"),
                ],
            }
        )


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
            datetime.now(TAIPEI).date() - timedelta(days=1),
            allowed_hostnames=ALLOWED,
        )


async def test_news_runner_refuses_future_edition() -> None:
    with pytest.raises(ValueError, match="current Taipei"):
        await run_news_edition(
            cast(async_sessionmaker[AsyncSession], None),
            cast(DeepSeekClient, None),
            datetime.now(TAIPEI).date() + timedelta(days=1),
            allowed_hostnames=ALLOWED,
        )
