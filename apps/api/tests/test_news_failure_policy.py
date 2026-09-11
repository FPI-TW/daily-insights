from datetime import UTC, datetime, timedelta

import httpx
import pytest

from daily_insights_api.modules.news.failures import classify_failure, retry_time


def response_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    response = httpx.Response(
        status, headers=headers, request=httpx.Request("GET", "https://example.test/news")
    )
    return httpx.HTTPStatusError("request failed", request=response.request, response=response)


@pytest.mark.parametrize("status", [401, 402, 403])
def test_model_account_failures_require_operator_recovery(status: int) -> None:
    failure = classify_failure(response_error(status), stage="selection", scope="provider:news")
    assert failure.action == "block"
    assert failure.code == f"provider_http_{status}"
    assert failure.http_status == status


def test_rate_limit_honors_http_date_even_when_longer_than_backoff() -> None:
    now = datetime(2026, 9, 11, 1, tzinfo=UTC)
    failure = classify_failure(
        response_error(429, {"Retry-After": "Fri, 11 Sep 2026 01:40:00 GMT"}),
        stage="feed",
        scope="source:example.test",
        now=now,
    )
    assert failure.action == "retry"
    assert retry_time(0, now, failure.retry_after) == now + timedelta(minutes=40)


@pytest.mark.parametrize("attempt,minutes", [(0, 5), (1, 15), (2, 30), (7, 30)])
def test_retry_cadence_is_bounded(attempt: int, minutes: int) -> None:
    now = datetime(2026, 9, 11, 1, tzinfo=UTC)
    assert retry_time(attempt, now) == now + timedelta(minutes=minutes)


def test_article_gone_is_skipped_without_blocking_model_or_other_sources() -> None:
    failure = classify_failure(response_error(410), stage="article", scope="source:example.test")
    assert failure.action == "skip"
    assert failure.code == "article_unavailable"


def test_unknown_bug_is_not_automatically_retried() -> None:
    failure = classify_failure(RuntimeError("private detail"), stage="selection")
    assert failure.action == "attention"
    assert "private detail" not in failure.model_dump_json()


@pytest.mark.parametrize(
    "status,action",
    [
        (408, "retry"),
        (429, "retry"),
        (500, "retry"),
        (502, "retry"),
        (503, "retry"),
        (504, "retry"),
        (401, "skip"),
        (403, "skip"),
        (404, "skip"),
        (410, "skip"),
    ],
)
def test_source_http_policy(status: int, action: str) -> None:
    assert classify_failure(response_error(status), stage="feed").action == action


@pytest.mark.parametrize(
    "message,code",
    [
        ("robots disallow feed", "robots_denied"),
        ("article response exceeds size limit", "response_too_large"),
        ("URL not in allowlist", "unsafe_destination"),
        ("article extraction too short or paywalled", "article_unreadable"),
    ],
)
def test_permanent_and_safety_rejections_never_retry(message: str, code: str) -> None:
    failure = classify_failure(ValueError(message), stage="article")
    assert failure.code == code and failure.action == "skip"
