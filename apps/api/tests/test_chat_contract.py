import json
import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import SystemRole
from daily_insights_api.modules.chat import api as chat_api
from daily_insights_api.modules.chat.api import (
    DISCLAIMER_BY_LOCALE,
    _append_disclaimer,
    _cross_page_snapshot,
    _current_detail_context,
    _current_index_context,
    _event,
    _limit_snapshot,
    _page_snapshot,
    _snapshot_digest,
)
from daily_insights_api.modules.chat.prompt import (
    BASIC_PROMPT,
    CHAT_CONTEXT_VERSION,
    build_chat_system_message,
)
from daily_insights_api.modules.chat.schemas import ChatStreamRequest
from daily_insights_api.modules.identity.auth import AuthContext
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.modules.model_runtime.api import CHAT_PROMPT_VERSION
from daily_insights_api.modules.model_runtime.service import _chat_configuration_desired
from daily_insights_api.modules.reports.models import ReportPublication


class _ScalarRows:
    def __init__(self, rows: list[ReportPublication]) -> None:
        self._rows = rows

    def all(self) -> list[ReportPublication]:
        return self._rows


class _SnapshotDatabase:
    def __init__(
        self, detail: ReportPublication, cross_page_reports: list[ReportPublication]
    ) -> None:
        self.detail = detail
        self.cross_page_reports = cross_page_reports
        self.latest_reports_statement: Any = None

    async def get(self, _: object, identifier: uuid.UUID) -> ReportPublication | None:
        return self.detail if identifier == self.detail.id else None

    async def scalars(self, statement: Any) -> _ScalarRows:
        self.latest_reports_statement = statement
        return _ScalarRows(self.cross_page_reports)


def _publication(
    market_code: str,
    *,
    content: object = None,
    summary: str | None = None,
    title: str | None = None,
) -> ReportPublication:
    return ReportPublication(
        id=uuid.uuid4(),
        pipeline_run_id=uuid.uuid4(),
        report_key="daily-market",
        market_code=market_code,
        edition_date=date(2026, 9, 3),
        revision=1,
        derivation_version="test.v1",
        content_schema_version="test.v1",
        input_digest="a" * 64,
        content={"report": content if content is not None else market_code},
        presentations={
            locale: {"title": title or f"{market_code} title", "summary": summary or market_code}
            for locale in ("zh-hant", "zh-hans", "en")
        },
    )


def _customer_context() -> AuthContext:
    return AuthContext(
        user=cast(
            User,
            SimpleNamespace(id=uuid.uuid4(), system_role=SystemRole.ORG_MEMBER),
        ),
        session=cast(Session, SimpleNamespace()),
        organization_id=uuid.uuid4(),
    )


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


def test_defensive_context_limit_preserves_cross_market_identity_with_escaped_content() -> None:
    escaped = '"\\繁體中文' * 20_000
    snapshot: dict[str, object] = {
        "version": CHAT_CONTEXT_VERSION,
        "kind": "report_detail",
        "current_page": {"content_excerpt": escaped},
        "cross_page_reports": [
            {
                "publication_id": str(uuid.uuid4()),
                "market_code": market_code,
                "edition_date": "2026-09-03",
                "title": escaped,
                "summary": escaped,
                "content_excerpt": escaped,
            }
            for market_code in ("global_macro_bonds", "crypto", "us_equity")
        ],
    }

    bounded = _limit_snapshot(snapshot)

    assert len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))) <= 60_000
    reports = bounded["cross_page_reports"]
    assert isinstance(reports, list)
    assert {report["market_code"] for report in reports if isinstance(report, dict)} == {
        "global_macro_bonds",
        "crypto",
        "us_equity",
    }
    assert all(report.get("content_excerpt") for report in reports if isinstance(report, dict))


@pytest.mark.asyncio
async def test_detail_snapshot_includes_only_authorized_cross_market_latest_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_report = _publication("global_macro_bonds", summary="Global report")
    us_report = _publication("us_equity", summary="US report")
    database = _SnapshotDatabase(global_report, [global_report, us_report])
    monkeypatch.setattr(
        chat_api,
        "visible_report_market_codes",
        AsyncMock(return_value=frozenset({"global_macro_bonds", "us_equity"})),
    )
    payload = ChatStreamRequest.model_validate(
        {
            "client_request_id": str(uuid.uuid4()),
            "locale": "en",
            "message": "How are US equities doing?",
            "page_context": {"kind": "report_detail", "publication_id": str(global_report.id)},
        }
    )

    snapshot, _ = await _page_snapshot(database, context=_customer_context(), payload=payload)  # type: ignore[arg-type]

    assert snapshot["version"] == CHAT_CONTEXT_VERSION
    current_page = snapshot["current_page"]
    assert isinstance(current_page, dict)
    assert current_page["market_code"] == "global_macro_bonds"
    cross_reports = snapshot["cross_page_reports"]
    assert isinstance(cross_reports, list)
    assert {report["market_code"] for report in cross_reports if isinstance(report, dict)} == {
        "global_macro_bonds",
        "us_equity",
    }
    assert "crypto" not in json.dumps(snapshot, ensure_ascii=False)
    assert database.latest_reports_statement is not None
    compiled = database.latest_reports_statement.compile()
    assert "daily-market" in compiled.params.values()
    assert any(
        {"global_macro_bonds", "us_equity"} <= set(value)
        for value in compiled.params.values()
        if isinstance(value, list)
    )


def test_cross_market_snapshot_fairly_retains_later_market_context() -> None:
    reports = [
        _publication("global_macro_bonds", content="a" * 200_000),
        _publication("crypto", content="crypto context"),
        _publication("us_equity", content="US equity context"),
    ]
    snapshot = _cross_page_snapshot(
        kind="report_detail",
        current_page={"market_code": "global_macro_bonds", "content_excerpt": "current"},
        reports=reports,
        locale="en",
    )

    assert len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))) <= 60_000
    cross_reports = snapshot["cross_page_reports"]
    assert isinstance(cross_reports, list)
    assert [report["market_code"] for report in cross_reports if isinstance(report, dict)] == [
        "global_macro_bonds",
        "crypto",
        "us_equity",
    ]
    assert all(
        report.get("summary") and report.get("content_excerpt")
        for report in cross_reports
        if isinstance(report, dict)
    )
    assert snapshot["truncated"] is True


def test_cross_market_snapshot_stays_bounded_after_json_escaping() -> None:
    reports = [
        _publication(market_code, content='"\\繁體中文' * 30_000)
        for market_code in ("global_macro_bonds", "crypto", "us_equity")
    ]
    snapshot = _cross_page_snapshot(
        kind="report_detail",
        current_page={"market_code": "global_macro_bonds", "content_excerpt": '"\\current'},
        reports=reports,
        locale="zh-hant",
    )

    assert len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))) <= 60_000
    cross_reports = snapshot["cross_page_reports"]
    assert isinstance(cross_reports, list)
    assert [report["market_code"] for report in cross_reports if isinstance(report, dict)] == [
        "global_macro_bonds",
        "crypto",
        "us_equity",
    ]
    assert all(
        report.get("content_excerpt") for report in cross_reports if isinstance(report, dict)
    )
    assert snapshot["truncated"] is True


def test_cross_market_metadata_clipping_sets_top_level_truncation() -> None:
    reports = [
        _publication(
            market_code,
            content="small content",
            title="t" * 501,
            summary="s" * 1_501,
        )
        for market_code in ("global_macro_bonds", "crypto", "us_equity")
    ]

    snapshot = _cross_page_snapshot(
        kind="report_detail",
        current_page={"market_code": "global_macro_bonds", "content_excerpt": "small"},
        reports=reports,
        locale="en",
    )

    assert len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))) <= 60_000
    assert snapshot["truncated"] is True


def test_current_detail_presentation_clipping_sets_top_level_truncation() -> None:
    report = _publication("global_macro_bonds", content="small content")
    report.presentations["en"] = {
        "title": "title",
        "summary": "summary",
        "rendered_sections": "p" * 2_001,
    }
    current_page = _current_detail_context(report, "en")

    snapshot = _cross_page_snapshot(
        kind="report_detail",
        current_page=current_page,
        reports=[],
        locale="en",
    )

    assert len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))) <= 60_000
    assert current_page["truncated"] is True
    assert snapshot["truncated"] is True


def test_reports_index_metadata_clipping_sets_top_level_truncation() -> None:
    current_page = _current_index_context(
        [
            {
                "publication_id": str(uuid.uuid4()),
                "market_code": "us_equity",
                "edition_date": "2026-09-03",
                "title": "t" * 501,
                "summary": "s" * 1_501,
            }
        ],
        [{"headline": "h" * 501, "summary": "n" * 1_501}],
    )

    snapshot = _cross_page_snapshot(
        kind="reports_index",
        current_page=current_page,
        reports=[],
        locale="en",
    )

    assert len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))) <= 60_000
    assert current_page["truncated"] is True
    assert snapshot["truncated"] is True


@pytest.mark.parametrize("locale", ["zh-hant", "zh-hans", "en"])
def test_system_prompt_renders_financial_scope_source_order_and_locale(locale: str) -> None:
    prompt = build_chat_system_message(
        locale=locale,
        snapshot={"version": CHAT_CONTEXT_VERSION, "current_page": {}, "cross_page_reports": []},
    )

    assert BASIC_PROMPT in prompt
    tier_1 = prompt.index("Tier 1:")
    tier_2 = prompt.index("Tier 2:")
    tier_3 = prompt.index("Tier 3:")
    assert tier_1 < tier_2 < tier_3
    assert "current_page and cross_page_reports are equal-priority authoritative sources" in prompt
    assert "MCP or tool results, when provided" in prompt
    assert "Skip this tier when no tool result exists" in prompt
    assert "model background knowledge, only as a clearly labelled non-live supplement" in prompt
    assert "For an unrelated question" not in prompt
    assert "not directly related to financial markets" not in prompt
    assert "此問題與金融市場無直接關聯" not in prompt
    assert "{disclaimer}" not in prompt
    assert "application appends one exactly once" in prompt


def test_chat_model_configuration_uses_the_cross_market_prompt_version() -> None:
    assert CHAT_PROMPT_VERSION == CHAT_CONTEXT_VERSION
    assert CHAT_PROMPT_VERSION == "page-context.cross-market.v5"
    assert _chat_configuration_desired(Settings())["prompt_version"] == CHAT_PROMPT_VERSION


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
            chat_model_api_key="CHANGE_ME_CHAT_MODEL_API_KEY",
        )
