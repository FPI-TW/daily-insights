import json
import uuid

import pytest
from pydantic import ValidationError

from daily_insights_api.core.config import Settings
from daily_insights_api.modules.chat.api import (
    DISCLAIMER_BY_LOCALE,
    _append_disclaimer,
    _event,
    _limit_snapshot,
    _snapshot_digest,
)
from daily_insights_api.modules.chat.schemas import ChatStreamRequest


def test_stream_request_requires_a_discriminated_page_context() -> None:
    request = ChatStreamRequest.model_validate(
        {
            "client_request_id": str(uuid.uuid4()),
            "locale": "en",
            "message": "What changed?",
            "page_context": {"kind": "report_detail", "publication_id": str(uuid.uuid4())},
        }
    )
    assert request.page_context.kind == "report_detail"
    with pytest.raises(ValidationError):
        ChatStreamRequest.model_validate(
            {
                "client_request_id": str(uuid.uuid4()),
                "locale": "en",
                "message": "What changed?",
                "page_context": {"kind": "podcast", "publication_id": str(uuid.uuid4())},
            }
        )


def test_context_limit_preserves_a_digestible_canonical_snapshot() -> None:
    snapshot: dict[str, object] = {
        "version": "page-context.v1",
        "kind": "report_detail",
        "content": "x" * 70_000,
    }
    bounded = _limit_snapshot(snapshot)
    assert bounded["truncated"] is True
    assert len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))) <= 60_000
    assert len(_snapshot_digest(bounded)) == 64


def test_sse_events_are_named_json_events() -> None:
    event = _event("done", {"status": "complete"}).decode()
    assert event == 'event: done\ndata: {"status":"complete"}\n\n'


@pytest.mark.parametrize("locale", ["zh-hant", "zh-hans", "en"])
def test_application_disclaimer_is_localized_and_appended_exactly_once(locale: str) -> None:
    assert DISCLAIMER_BY_LOCALE["zh-hant"] == (
        "（內容基於公開資訊及內部分析報告，僅供參考，不構成投資建議。）"  # noqa: RUF001
    )
    chunks = ["Answer"]
    suffix = _append_disclaimer(chunks, locale)
    assert suffix == f"\n\n{DISCLAIMER_BY_LOCALE[locale]}"
    assert "".join(chunks).endswith(DISCLAIMER_BY_LOCALE[locale])
    assert _append_disclaimer(chunks, locale) is None


def test_chat_openapi_declares_sse_response_contract() -> None:
    from daily_insights_api.web.app import create_app

    operation = create_app(Settings(environment="test")).openapi()["paths"]["/api/v1/chat/stream"][
        "post"
    ]
    content = operation["responses"]["200"]["content"]
    assert set(content) == {"text/event-stream"}


def test_enabled_chat_requires_a_non_placeholder_key_in_production() -> None:
    with pytest.raises(ValidationError, match="chat_model_api_key"):
        Settings(
            environment="production",
            database_url="postgresql+psycopg://app:secret@example.invalid/app",
            session_secret="s" * 32,
            password_pepper="p" * 32,
            r2_endpoint_url="https://account.r2.cloudflarestorage.com",
            r2_bucket_name="daily-insights-production",
            r2_access_key_id="r2-access-key",
            r2_secret_access_key="r2-secret-key",
            chat_enabled=True,
        )
