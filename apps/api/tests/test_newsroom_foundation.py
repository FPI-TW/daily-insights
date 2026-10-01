from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.newsroom import clock
from daily_insights_api.modules.newsroom.contracts import AnalysisResult, TriageResult
from daily_insights_api.modules.newsroom.queue import backoff


def test_edition_window_cuts_over_at_eight_taipei() -> None:
    # 07:59 Taipei on 10-01 is 23:59 UTC on 09-30.
    assert clock.edition_date_for(datetime(2026, 9, 30, 23, 59, tzinfo=UTC)) == date(2026, 10, 1)
    # 08:00 Taipei belongs to the next day's edition.
    assert clock.edition_date_for(datetime(2026, 10, 1, 0, 0, tzinfo=UTC)) == date(2026, 10, 2)
    start, end = clock.window(date(2026, 10, 1))
    assert start == datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 1, 0, 0, tzinfo=UTC)


def test_daily_deadlines_are_taipei_nine_and_noon() -> None:
    assert clock.auto_publish_at(date(2026, 10, 1)) == datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
    assert clock.late_fill_deadline(date(2026, 10, 1)) == datetime(2026, 10, 1, 4, 0, tzinfo=UTC)


def test_backoff_doubles_caps_and_respects_retry_after() -> None:
    assert [backoff(n) for n in (1, 2, 3, 4)] == [timedelta(minutes=m) for m in (1, 2, 4, 8)]
    assert backoff(20) == timedelta(minutes=60)
    assert backoff(1, timedelta(minutes=5)) == timedelta(minutes=5)


def test_triage_result_accepts_match_or_new_event() -> None:
    scores = {"global": 80, "tw_equity": 10, "us_equity": 40}
    matched = TriageResult.model_validate(
        {
            "relevant": True,
            "topic": "economy",
            "market_scores": scores,
            "event": {"match": "e1"},
        }
    )
    assert matched.market_scores.as_dict() == scores
    created = TriageResult.model_validate(
        {"relevant": True, "topic": "economy", "market_scores": scores, "event": {"new": "Fed"}}
    )
    assert created.event is not None
    with pytest.raises(ValidationError):
        TriageResult.model_validate(
            {"relevant": True, "topic": "economy", "market_scores": {**scores, "global": 101}}
        )


def test_analysis_result_requires_a_market_why() -> None:
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate({"headline": "標題", "summary": "摘要", "why": []})


def test_newsroom_settings_reject_placeholder_keys() -> None:
    with pytest.raises(ValidationError, match="newsroom_llm_api_key"):
        Settings(
            runtime_role="newsroom-worker",
            newsroom_enabled=True,
            newsroom_llm_api_key="change-me",
            newsroom_embedding_api_key="sk-real",
        )
    with pytest.raises(ValidationError, match=r"hooks\.slack\.com"):
        Settings(
            runtime_role="newsroom-worker",
            newsroom_enabled=True,
            newsroom_llm_api_key="sk-real",
            newsroom_embedding_api_key="sk-real",
            newsroom_slack_webhook_url="https://example.com/hook",
        )
