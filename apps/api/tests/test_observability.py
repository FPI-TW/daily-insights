import json
import logging

import pytest

from daily_insights_api.core.observability import emit_event, sanitize_fields


def test_sensitive_observability_fields_are_redacted_recursively() -> None:
    assert sanitize_fields(
        {
            "request_id": "request-1",
            "password": "do-not-log",
            "nested": {"R2_SECRET_ACCESS_KEY": "do-not-log"},
        }
    ) == {
        "request_id": "request-1",
        "password": "[REDACTED]",
        "nested": {"R2_SECRET_ACCESS_KEY": "[REDACTED]"},
    }


def test_structured_event_does_not_log_secret_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="daily_insights"):
        emit_event("asset.signed_url.issued", asset_id="asset-1", token="secret-value")

    record = caplog.records[-1]
    payload = json.loads(record.message)
    assert payload == {
        "event": "asset.signed_url.issued",
        "asset_id": "asset-1",
        "token": "[REDACTED]",
    }
    assert "secret-value" not in record.message
