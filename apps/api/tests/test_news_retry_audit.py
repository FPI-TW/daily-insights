import uuid

import pytest

from daily_insights_api.modules.news.models import NewsGenerationAudit
from daily_insights_api.modules.news.service import _edition_status, _failed_audit, _retry


def _attempt_audit(error: Exception) -> NewsGenerationAudit:
    return _failed_audit(
        uuid.uuid4(),
        "summary",
        "en",
        "a" * 64,
        "deepseek-chat",
        error,
    )


async def test_retry_records_one_digest_only_failed_attempt_before_success() -> None:
    attempts = 0
    audits: list[NewsGenerationAudit] = []

    async def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("provider temporary failure")
        return "ok"

    assert await _retry(call, lambda error: audits.append(_attempt_audit(error))) == "ok"
    assert attempts == 2
    assert [(audit.stage, audit.status, audit.error_code) for audit in audits] == [
        ("summary", "failed", "ValueError")
    ]
    assert audits[0].input_digest == "a" * 64
    assert {"prompt", "body", "article_text"}.isdisjoint(
        NewsGenerationAudit.__table__.columns.keys()
    )


async def test_retry_records_every_terminal_failure_and_results_in_unavailable_status() -> None:
    audits: list[NewsGenerationAudit] = []

    async def call() -> str:
        raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await _retry(call, lambda error: audits.append(_attempt_audit(error)))
    assert len(audits) == 2
    assert all(audit.stage == "summary" and audit.status == "failed" for audit in audits)
    assert all(len(audit.input_digest) == 64 for audit in audits)
    assert _edition_status(0) == ("unavailable", "0/5 stories completed")
