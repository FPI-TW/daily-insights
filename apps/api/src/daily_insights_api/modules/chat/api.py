"""Page-context customer chat and read-only administrative review APIs."""

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.enums import GenerationStatus, MessageRole, SystemRole
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.chat.models import Conversation, Message
from daily_insights_api.modules.chat.prompt import (
    CHAT_CONTEXT_VERSION,
    build_chat_system_message,
)
from daily_insights_api.modules.chat.provider import ChatProvider, ProviderMetadata
from daily_insights_api.modules.chat.schemas import (
    ChatConversationDetailResponse,
    ChatConversationListItem,
    ChatConversationListResponse,
    ChatGenerationResponse,
    ChatMessageResponse,
    ChatStreamRequest,
    ReportDetailContext,
    ReportsIndexContext,
)
from daily_insights_api.modules.identity.api import AuthContext, User, require_csrf, require_roles
from daily_insights_api.modules.model_runtime.api import (
    ActiveModelConfigurationRecord as ActiveModelConfiguration,
)
from daily_insights_api.modules.model_runtime.api import (
    GenerationRecord,
    ModelConfiguration,
)
from daily_insights_api.modules.news.api import NewsEdition, NewsItem, NewsPresentation
from daily_insights_api.modules.reports.api import (
    LAUNCH_MARKET_ORDER,
    ReportPublication,
    visible_report_market_codes,
)
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(tags=["chat"])

DISCLAIMER_BY_LOCALE = {
    "zh-hant": "（內容基於公開資訊及內部分析報告，僅供參考，不構成投資建議。）",  # noqa: RUF001
    "zh-hans": "（内容基于公开信息及内部分析报告，仅供参考，不构成投资建议。）",  # noqa: RUF001
    "en": (
        "(Content is based on public information and internal analysis reports, "
        "is for reference only, and does not constitute investment advice.)"
    ),
}


def require_chat_customer(context: Annotated[AuthContext, Depends(require_csrf)]) -> AuthContext:
    """Chat is a protected customer mutation, including the password-change gate."""
    if context.user.must_change_password:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "password change required",
            headers={"X-Password-Change-Required": "true"},
        )
    return context


Customer = Annotated[AuthContext, Depends(require_chat_customer)]
Admin = Annotated[AuthContext, Depends(require_roles(SystemRole.ADMIN))]


def _event(name: str, data: dict[str, object]) -> bytes:
    return (
        f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n".encode()
    )


def _append_disclaimer(chunks: list[str], locale: str) -> str | None:
    """Append the application-owned disclaimer exactly once to a visible reply."""
    disclaimer = DISCLAIMER_BY_LOCALE.get(locale, DISCLAIMER_BY_LOCALE["en"])
    content = "".join(chunks).rstrip()
    if content.endswith(disclaimer):
        return None
    suffix = f"\n\n{disclaimer}" if content else disclaimer
    if not content:
        chunks.clear()
    chunks.append(suffix)
    return suffix


def _require_customer(context: AuthContext) -> uuid.UUID:
    if context.user.system_role != SystemRole.ORG_MEMBER or context.organization_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organization membership required")
    return context.organization_id


def _snapshot_digest(snapshot: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


SNAPSHOT_MAX_CHARS = 60_000
_CURRENT_PAGE_MAX_CHARS = 30_000
_CURRENT_INDEX_MAX_CHARS = 26_000
_CROSS_PAGE_TITLE_MAX_CHARS = 500
_CROSS_PAGE_SUMMARY_MAX_CHARS = 1_500


def _serialized_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _clip_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value if len(value) <= limit else f"{value[: max(0, limit - 1)]}…"


def _text_was_truncated(value: object, limit: int) -> bool:
    return isinstance(value, str) and len(value) > limit


def _json_excerpt(value: object, limit: int) -> str:
    if limit <= 0:
        return "…"
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return serialized if len(serialized) <= limit else f"{serialized[: max(0, limit - 1)]}…"


def _json_was_truncated(value: object, limit: int) -> bool:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) > limit


def _presentation_fields(presentation: object, *, summary_limit: int) -> dict[str, str | None]:
    if not isinstance(presentation, dict):
        return {"title": None, "summary": None}
    return {
        "title": _clip_text(presentation.get("title"), _CROSS_PAGE_TITLE_MAX_CHARS),
        "summary": _clip_text(presentation.get("summary"), summary_limit),
    }


def _presentation_metadata_was_truncated(presentation: object, *, summary_limit: int) -> bool:
    if not isinstance(presentation, dict):
        return False
    return _text_was_truncated(
        presentation.get("title"), _CROSS_PAGE_TITLE_MAX_CHARS
    ) or _text_was_truncated(presentation.get("summary"), summary_limit)


def _minimal_current_page(current_page: object) -> dict[str, object]:
    """Retain page identity while making room for every authorized market's context."""
    if not isinstance(current_page, dict):
        return {"truncated": True, "excerpt": "…"}
    detail_keys = ("publication_id", "market_code", "edition_date")
    if all(key in current_page for key in detail_keys):
        return {key: _clip_text(current_page[key], 128) for key in detail_keys} | {
            "truncated": True
        }
    reports = current_page.get("reports")
    news = current_page.get("news")
    return {
        "truncated": True,
        "reports": [
            {
                key: _clip_text(item.get(key), 128)
                for key in ("publication_id", "market_code", "edition_date")
            }
            for item in reports
            if isinstance(item, dict)
        ]
        if isinstance(reports, list)
        else [],
        "news": [
            {"headline": _clip_text(item.get("headline"), 100)}
            for item in news[:20]
            if isinstance(item, dict)
        ]
        if isinstance(news, list)
        else [],
    }


def _minimal_cross_page_reports(snapshot: dict[str, object]) -> list[dict[str, object]]:
    reports = snapshot.get("cross_page_reports")
    if not isinstance(reports, list):
        return []
    # Formal launch markets are a fixed three-market set. Limit malformed defensive input to
    # that product invariant, while retaining every real authorized market identity.
    return [
        {
            "publication_id": _clip_text(item.get("publication_id"), 128),
            "market_code": _clip_text(item.get("market_code"), 128),
            "edition_date": _clip_text(item.get("edition_date"), 128),
            "title": _clip_text(item.get("title"), 128),
            "summary": _clip_text(item.get("summary"), 256),
            "content_excerpt": "…",
        }
        for item in reports[: len(LAUNCH_MARKET_ORDER)]
        if isinstance(item, dict)
    ]


def _shrink_snapshot_strings(snapshot: dict[str, object]) -> None:
    """Shrink non-identity text deterministically until the final JSON fits its hard cap."""
    paths: list[tuple[dict[str, object], str]] = []
    current_page = snapshot.get("current_page")
    if isinstance(current_page, dict):
        paths.extend(
            (current_page, key) for key in ("content_excerpt", "presentation_excerpt", "excerpt")
        )
    reports = snapshot.get("cross_page_reports")
    if isinstance(reports, list):
        for report in reports:
            if isinstance(report, dict):
                paths.extend((report, key) for key in ("content_excerpt", "summary", "title"))
    while _serialized_size(snapshot) > SNAPSHOT_MAX_CHARS:
        candidates = [
            (len(value), index, container, key)
            for index, (container, key) in enumerate(paths)
            if isinstance((value := container.get(key)), str) and len(value) > 1
        ]
        if not candidates:
            return
        _, _, container, key = max(candidates, key=lambda candidate: (candidate[0], -candidate[1]))
        value = container[key]
        assert isinstance(value, str)
        container[key] = _clip_text(value, max(1, len(value) // 2))


def _limit_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    """Defensively bound persisted context using final serialized JSON size, not source length."""
    if _serialized_size(snapshot) <= SNAPSHOT_MAX_CHARS:
        return snapshot
    fallback: dict[str, object] = {
        "version": _clip_text(snapshot.get("version"), 128) or CHAT_CONTEXT_VERSION,
        "kind": _clip_text(snapshot.get("kind"), 128),
        "truncated": True,
        "current_page": _minimal_current_page(snapshot.get("current_page")),
        "cross_page_reports": _minimal_cross_page_reports(snapshot),
    }
    _shrink_snapshot_strings(fallback)
    if _serialized_size(fallback) <= SNAPSHOT_MAX_CHARS:
        return fallback
    fallback["current_page"] = {"truncated": True, "excerpt": "…"}
    _shrink_snapshot_strings(fallback)
    assert _serialized_size(fallback) <= SNAPSHOT_MAX_CHARS
    return fallback


def _current_detail_context(publication: ReportPublication, locale: str) -> dict[str, object]:
    presentation = publication.presentations.get(locale)
    fields = _presentation_fields(presentation, summary_limit=2_000)
    content_limit = _CURRENT_PAGE_MAX_CHARS - 10_000
    presentation_limit = 2_000
    content_truncated = _json_was_truncated(publication.content, content_limit)
    presentation_truncated = _json_was_truncated(presentation, presentation_limit)
    metadata_truncated = _presentation_metadata_was_truncated(presentation, summary_limit=2_000)
    return {
        "publication_id": str(publication.id),
        "market_code": publication.market_code,
        "edition_date": publication.edition_date.isoformat(),
        **fields,
        "content_excerpt": _json_excerpt(publication.content, content_limit),
        "content_truncated": content_truncated,
        "presentation_excerpt": _json_excerpt(presentation, presentation_limit),
        "truncated": content_truncated or presentation_truncated or metadata_truncated,
    }


def _current_index_context(
    reports: list[dict[str, object]], news: list[dict[str, object]]
) -> dict[str, object]:
    """Keep each current-page item recognizable when the index has unusually long copy."""
    metadata_truncated = any(
        _text_was_truncated(item.get("title"), _CROSS_PAGE_TITLE_MAX_CHARS)
        or _text_was_truncated(item.get("summary"), _CROSS_PAGE_SUMMARY_MAX_CHARS)
        for item in reports
    ) or any(
        _text_was_truncated(item.get("headline"), _CROSS_PAGE_TITLE_MAX_CHARS)
        or _text_was_truncated(item.get("summary"), _CROSS_PAGE_SUMMARY_MAX_CHARS)
        for item in news
    )
    normalized_reports: list[dict[str, object]] = [
        {
            **{key: item[key] for key in ("publication_id", "market_code", "edition_date")},
            "title": _clip_text(item.get("title"), _CROSS_PAGE_TITLE_MAX_CHARS),
            "summary": _clip_text(item.get("summary"), _CROSS_PAGE_SUMMARY_MAX_CHARS),
        }
        for item in reports
    ]
    normalized_news: list[dict[str, object]] = [
        {
            "headline": _clip_text(item.get("headline"), _CROSS_PAGE_TITLE_MAX_CHARS),
            "summary": _clip_text(item.get("summary"), _CROSS_PAGE_SUMMARY_MAX_CHARS),
        }
        for item in news
    ]
    current_page: dict[str, object] = {"reports": normalized_reports, "news": normalized_news}
    if _serialized_size(current_page) <= _CURRENT_INDEX_MAX_CHARS:
        return {**current_page, "truncated": True} if metadata_truncated else current_page
    for item in normalized_reports:
        item["title"] = ""
        item["summary"] = ""
    for item in normalized_news:
        item["headline"] = ""
        item["summary"] = ""
    remaining = _CURRENT_INDEX_MAX_CHARS - _serialized_size(current_page)
    text_slots = len(normalized_reports) * 2 + len(normalized_news) * 2
    if text_slots and remaining > 0:
        per_text = remaining // text_slots
        for source, target in zip(reports, normalized_reports, strict=True):
            target["title"] = _clip_text(source.get("title"), per_text)
            target["summary"] = _clip_text(source.get("summary"), per_text)
        for source, target in zip(news, normalized_news, strict=True):
            target["headline"] = _clip_text(source.get("headline"), per_text)
            target["summary"] = _clip_text(source.get("summary"), per_text)
    if _serialized_size(current_page) <= _CURRENT_INDEX_MAX_CHARS:
        return {**current_page, "truncated": True}
    return {
        "truncated": True,
        "reports": [
            {key: item[key] for key in ("publication_id", "market_code", "edition_date")}
            for item in normalized_reports
        ],
        "news": [
            {"headline": _clip_text(item.get("headline"), 100)} for item in normalized_news[:20]
        ],
    }


def _cross_page_report_context(publication: ReportPublication, locale: str) -> dict[str, object]:
    return {
        "publication_id": str(publication.id),
        "market_code": publication.market_code,
        "edition_date": publication.edition_date.isoformat(),
        **_presentation_fields(
            publication.presentations.get(locale), summary_limit=_CROSS_PAGE_SUMMARY_MAX_CHARS
        ),
    }


def _cross_page_snapshot(
    *,
    kind: str,
    current_page: dict[str, object],
    reports: list[ReportPublication],
    locale: str,
) -> dict[str, object]:
    """Keep current-page data first while allocating each other market a fair excerpt."""
    snapshot: dict[str, object] = {
        "version": CHAT_CONTEXT_VERSION,
        "kind": kind,
        "current_page": current_page,
        "cross_page_reports": [_cross_page_report_context(report, locale) for report in reports],
        "truncated": False,
    }
    cross_contexts = snapshot["cross_page_reports"]
    assert isinstance(cross_contexts, list)
    cross_metadata_truncated = any(
        _presentation_metadata_was_truncated(
            report.presentations.get(locale), summary_limit=_CROSS_PAGE_SUMMARY_MAX_CHARS
        )
        for report in reports
    )
    current_page_truncated = bool(current_page.get("truncated")) or bool(
        current_page.get("content_truncated")
    )
    # Every report first receives its identity and a bounded summary. Remaining space is split
    # evenly, so a large first report cannot consume later markets' authoritative context.
    for report_context in cross_contexts:
        assert isinstance(report_context, dict)
        report_context["content_excerpt"] = "…"
    if _serialized_size(snapshot) > SNAPSHOT_MAX_CHARS:
        snapshot["current_page"] = _minimal_current_page(current_page)
        current_page_truncated = True
    if reports and _serialized_size(snapshot) <= SNAPSHOT_MAX_CHARS:
        low, high, best = 1, max(_serialized_size(report.content) for report in reports), 1
        while low <= high:
            per_report = (low + high) // 2
            for report, report_context in zip(reports, cross_contexts, strict=True):
                assert isinstance(report_context, dict)
                report_context["content_excerpt"] = _json_excerpt(report.content, per_report)
            if _serialized_size(snapshot) <= SNAPSHOT_MAX_CHARS:
                best = per_report
                low = per_report + 1
            else:
                high = per_report - 1
        for report, report_context in zip(reports, cross_contexts, strict=True):
            assert isinstance(report_context, dict)
            report_context["content_excerpt"] = _json_excerpt(report.content, best)
    snapshot["truncated"] = (
        current_page_truncated
        or cross_metadata_truncated
        or any(
            _json_was_truncated(report.content, len(context.get("content_excerpt", "")))
            for report, context in zip(reports, cross_contexts, strict=True)
            if isinstance(context, dict)
        )
    )
    return _limit_snapshot(snapshot)


async def _latest_cross_page_reports(
    database: AsyncSession, visible_markets: frozenset[str]
) -> list[ReportPublication]:
    """Fetch only the latest immutable daily-market publication for each allowed formal market."""
    allowed = tuple(code for code in LAUNCH_MARKET_ORDER if code in visible_markets)
    if not allowed:
        return []
    latest_rank = (
        func.row_number()
        .over(
            partition_by=ReportPublication.market_code,
            order_by=(
                ReportPublication.edition_date.desc(),
                ReportPublication.revision.desc(),
                ReportPublication.published_at.desc(),
            ),
        )
        .label("latest_rank")
    )
    latest = (
        select(ReportPublication.id.label("publication_id"), latest_rank)
        .where(
            ReportPublication.report_key == "daily-market",
            ReportPublication.market_code.in_(allowed),
        )
        .subquery()
    )
    return list(
        (
            await database.scalars(
                select(ReportPublication)
                .join(latest, latest.c.publication_id == ReportPublication.id)
                .where(latest.c.latest_rank == 1)
                .order_by(ReportPublication.market_code)
            )
        ).all()
    )


async def _page_snapshot(
    database: AsyncSession, *, context: AuthContext, payload: ChatStreamRequest
) -> tuple[dict[str, object], str | None]:
    _require_customer(context)
    visible = await visible_report_market_codes(database, context)
    cross_page_reports = await _latest_cross_page_reports(database, visible)
    page = payload.page_context
    if isinstance(page, ReportsIndexContext):
        rows = (
            await database.scalars(
                select(ReportPublication).where(ReportPublication.id.in_(page.publication_ids))
            )
        ).all()
        if len(rows) != len(set(page.publication_ids)) or any(
            row.market_code not in visible for row in rows
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "report context unavailable")
        reports: list[dict[str, object]] = []
        for publication in rows:
            presentation = publication.presentations.get(payload.locale)
            if not isinstance(presentation, dict):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "report context unavailable")
            reports.append(
                {
                    "publication_id": str(publication.id),
                    "market_code": publication.market_code,
                    "edition_date": publication.edition_date.isoformat(),
                    "summary": presentation.get("summary"),
                    "title": presentation.get("title"),
                }
            )
        news: list[dict[str, object]] = []
        if page.news_edition_id is not None:
            edition = await database.get(NewsEdition, page.news_edition_id)
            if edition is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "news context unavailable")
            result = await database.execute(
                select(NewsItem, NewsPresentation)
                .join(
                    NewsPresentation,
                    (NewsPresentation.item_id == NewsItem.id)
                    & (NewsPresentation.locale == payload.locale),
                )
                .where(NewsItem.edition_id == edition.id)
                .order_by(NewsItem.rank)
            )
            news = [
                {"headline": presentation.headline, "summary": presentation.summary}
                for _, presentation in result
            ]
        return (
            _cross_page_snapshot(
                kind=page.kind,
                current_page=_current_index_context(reports, news),
                reports=cross_page_reports,
                locale=payload.locale,
            ),
            None,
        )
    assert isinstance(page, ReportDetailContext)
    detail_publication = await database.get(ReportPublication, page.publication_id)
    if detail_publication is None or detail_publication.market_code not in visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report context unavailable")
    return (
        _cross_page_snapshot(
            kind=page.kind,
            current_page=_current_detail_context(detail_publication, payload.locale),
            reports=cross_page_reports,
            locale=payload.locale,
        ),
        detail_publication.manifest_version,
    )


async def _active_config(database: AsyncSession) -> ModelConfiguration:
    config = await database.scalar(
        select(ModelConfiguration)
        .join(
            ActiveModelConfiguration,
            ActiveModelConfiguration.model_configuration_id == ModelConfiguration.id,
        )
        .where(ActiveModelConfiguration.singleton_id == 1)
    )
    if config is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "chat model is unavailable")
    return config


async def _previous_messages(
    database: AsyncSession, conversation_id: uuid.UUID, before_sequence: int
) -> tuple[list[dict[str, str]], bool]:
    values = list(
        (
            await database.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.status != GenerationStatus.PENDING,
                    Message.sequence_number < before_sequence,
                )
                .order_by(Message.sequence_number.desc())
                .limit(40)  # 20 user/assistant turns
            )
        ).all()
    )
    values.reverse()
    budget = 40_000
    result: list[dict[str, str]] = []
    truncated = False
    for message in reversed(values):
        if len(message.content) > budget:
            truncated = True
            continue
        budget -= len(message.content)
        result.append({"role": message.role.value, "content": message.content})
    result.reverse()
    return result, truncated


async def _create_pending_turn(
    database: AsyncSession,
    *,
    context: AuthContext,
    payload: ChatStreamRequest,
    snapshot: dict[str, object],
    report_version: str | None,
) -> tuple[Conversation, Message, Message, GenerationRecord, bool]:
    organization_id = _require_customer(context)
    existing = await database.scalar(
        select(Message).where(Message.client_request_id == payload.client_request_id)
    )
    if existing is not None:
        conversation = await database.get(Conversation, existing.conversation_id)
        if (
            conversation is None
            or conversation.organization_id != organization_id
            or conversation.user_id != context.user.id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        assistant = await database.scalar(
            select(Message).where(Message.reply_to_message_id == existing.id)
        )
        generation = (
            await database.scalar(
                select(GenerationRecord).where(GenerationRecord.message_id == assistant.id)
            )
            if assistant is not None
            else None
        )
        if assistant is None or generation is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "incomplete idempotency record")
        if assistant.status == GenerationStatus.PENDING:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "conversation already has a pending reply"
            )
        return conversation, existing, assistant, generation, True
    if payload.conversation_id is None:
        conversation = Conversation(organization_id=organization_id, user_id=context.user.id)
        database.add(conversation)
        await database.flush()
        sequence = 0
    else:
        conversation = await database.scalar(
            select(Conversation)
            .where(
                Conversation.id == payload.conversation_id,
                Conversation.organization_id == organization_id,
                Conversation.user_id == context.user.id,
            )
            .with_for_update()
        )
        if conversation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        pending = await database.scalar(
            select(Message.id).where(
                Message.conversation_id == conversation.id,
                Message.role == MessageRole.ASSISTANT,
                Message.status == GenerationStatus.PENDING,
            )
        )
        if pending is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "conversation already has a pending reply"
            )
        sequence = (
            int(
                await database.scalar(
                    select(func.coalesce(func.max(Message.sequence_number), -1)).where(
                        Message.conversation_id == conversation.id
                    )
                )
                or -1
            )
            + 1
        )
    config = await _active_config(database)
    user = Message(
        conversation_id=conversation.id,
        sequence_number=sequence,
        role=MessageRole.USER,
        content=payload.message,
        client_request_id=payload.client_request_id,
        status=GenerationStatus.COMPLETE,
    )
    database.add(user)
    await database.flush()
    assistant = Message(
        conversation_id=conversation.id,
        sequence_number=sequence + 1,
        role=MessageRole.ASSISTANT,
        content="",
        reply_to_message_id=user.id,
        status=GenerationStatus.PENDING,
    )
    database.add(assistant)
    await database.flush()
    generation = GenerationRecord(
        message_id=assistant.id,
        model_configuration_id=config.id,
        model_configuration_version=config.version,
        provider=config.provider,
        requested_model=config.requested_model,
        prompt_version=config.prompt_version,
        context_version=str(snapshot["version"]),
        report_version=report_version,
        parameters=config.parameters,
        context_snapshot=snapshot,
        context_digest=_snapshot_digest(snapshot),
        context_truncated=bool(snapshot.get("truncated")),
        status=GenerationStatus.PENDING,
    )
    database.add(generation)
    await database.commit()
    return conversation, user, assistant, generation, False


async def _terminalize(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    assistant_id: uuid.UUID,
    generation_id: uuid.UUID,
    content: str,
    terminal: GenerationStatus,
    metadata: ProviderMetadata,
    error_code: str | None = None,
) -> None:
    async with session_factory.begin() as database:
        assistant = await database.scalar(
            select(Message).where(Message.id == assistant_id).with_for_update()
        )
        generation = await database.scalar(
            select(GenerationRecord).where(GenerationRecord.id == generation_id).with_for_update()
        )
        if assistant is None or generation is None or assistant.status != GenerationStatus.PENDING:
            return
        assistant.content = content
        assistant.status = terminal
        generation.status = terminal
        generation.resolved_model = metadata.resolved_model
        generation.provider_request_id = metadata.provider_request_id
        generation.input_tokens = metadata.input_tokens
        generation.output_tokens = metadata.output_tokens
        generation.latency_ms = metadata.latency_ms
        generation.error_code = error_code


async def _cleanup_cancelled_turn(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    assistant_id: uuid.UUID,
    generation_id: uuid.UUID,
    content: str,
    locale: str,
    metadata: ProviderMetadata,
) -> None:
    """Durably release the single-pending constraint after any generator cancellation."""
    stored_chunks = [content]
    if content:
        _append_disclaimer(stored_chunks, locale)
    task = asyncio.create_task(
        _terminalize(
            session_factory,
            assistant_id=assistant_id,
            generation_id=generation_id,
            content="".join(stored_chunks),
            terminal=GenerationStatus.PARTIAL if content else GenerationStatus.ERROR,
            metadata=metadata,
            error_code="cancelled",
        )
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # A repeated cancellation cannot cancel the independent database task.
        pass


@router.post(
    "/api/v1/chat/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_chat(
    payload: ChatStreamRequest,
    request: Request,
    context: Customer,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> StreamingResponse:
    _require_customer(context)
    if not request.app.state.settings.chat_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "chat is disabled")
    snapshot, report_version = await _page_snapshot(database, context=context, payload=payload)
    try:
        conversation, user, assistant, generation, replay = await _create_pending_turn(
            database,
            context=context,
            payload=payload,
            snapshot=snapshot,
            report_version=report_version,
        )
    except IntegrityError as error:
        await database.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "concurrent chat turn") from error
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory

    chunks: list[str] = []
    stream = None
    started = perf_counter()

    async def lifecycle() -> AsyncIterator[bytes]:
        nonlocal stream
        yield _event(
            "meta",
            {
                "conversation_id": conversation.id,
                "user_message_id": user.id,
                "assistant_message_id": assistant.id,
                "replay": replay,
            },
        )
        if replay:
            if assistant.content:
                yield _event("delta", {"text": assistant.content})
            yield _event("done", {"status": assistant.status.value, "message_id": assistant.id})
            return
        history, history_truncated = await _previous_messages(
            database, conversation.id, user.sequence_number
        )
        prompt = {
            "role": "system",
            "content": build_chat_system_message(locale=payload.locale, snapshot=snapshot),
        }
        provider: ChatProvider | None = getattr(request.app.state, "chat_provider", None)
        if provider is None:
            await _terminalize(
                session_factory,
                assistant_id=assistant.id,
                generation_id=generation.id,
                content="",
                terminal=GenerationStatus.ERROR,
                metadata=ProviderMetadata(),
                error_code="provider_unavailable",
            )
            yield _event("error", {"code": "provider_unavailable"})
            return
        started = perf_counter()
        try:
            stream = await provider.stream(
                model=generation.requested_model,
                messages=[prompt, *history, {"role": "user", "content": payload.message}],
                timeout_seconds=request.app.state.settings.chat_timeout_seconds,
            )
            async with asyncio.timeout(request.app.state.settings.chat_timeout_seconds):
                async for chunk in stream:
                    if await request.is_disconnected():
                        break
                    chunks.append(chunk)
                    yield _event("delta", {"text": chunk})
            disconnected = await request.is_disconnected()
            terminal = GenerationStatus.PARTIAL if disconnected else GenerationStatus.COMPLETE
            disclaimer = (
                _append_disclaimer(chunks, payload.locale) if chunks or not disconnected else None
            )
            if not disconnected and disclaimer is not None:
                yield _event("delta", {"text": disclaimer})
            metadata = stream.metadata
            metadata.latency_ms = metadata.latency_ms or max(
                0, round((perf_counter() - started) * 1000)
            )
            await _terminalize(
                session_factory,
                assistant_id=assistant.id,
                generation_id=generation.id,
                content="".join(chunks),
                terminal=terminal,
                metadata=metadata,
            )
            if not disconnected:
                yield _event(
                    "done",
                    {
                        "status": terminal.value,
                        "message_id": assistant.id,
                        "history_truncated": history_truncated,
                    },
                )
        except TimeoutError:
            metadata = stream.metadata if stream is not None else ProviderMetadata()
            metadata.latency_ms = max(0, round((perf_counter() - started) * 1000))
            disclaimer = _append_disclaimer(chunks, payload.locale) if chunks else None
            if disclaimer is not None:
                yield _event("delta", {"text": disclaimer})
            terminal = GenerationStatus.PARTIAL if chunks else GenerationStatus.ERROR
            await _terminalize(
                session_factory,
                assistant_id=assistant.id,
                generation_id=generation.id,
                content="".join(chunks),
                terminal=terminal,
                metadata=metadata,
                error_code="timeout",
            )
            yield _event("error", {"code": "timeout", "partial": bool(chunks)})
        except asyncio.CancelledError:
            # Disconnect and Stop cancellation must not strand the unique
            # pending assistant row. Shield the independent DB transaction so
            # it survives this generator task being cancelled.
            metadata = stream.metadata if stream is not None else ProviderMetadata()
            metadata.latency_ms = max(0, round((perf_counter() - started) * 1000))
            await _cleanup_cancelled_turn(
                session_factory,
                assistant_id=assistant.id,
                generation_id=generation.id,
                content="".join(chunks),
                locale=payload.locale,
                metadata=metadata,
            )
            raise
        except Exception:
            metadata = stream.metadata if stream is not None else ProviderMetadata()
            metadata.latency_ms = max(0, round((perf_counter() - started) * 1000))
            disclaimer = _append_disclaimer(chunks, payload.locale) if chunks else None
            if disclaimer is not None:
                yield _event("delta", {"text": disclaimer})
            terminal = GenerationStatus.PARTIAL if chunks else GenerationStatus.ERROR
            await _terminalize(
                session_factory,
                assistant_id=assistant.id,
                generation_id=generation.id,
                content="".join(chunks),
                terminal=terminal,
                metadata=metadata,
                error_code="provider_error",
            )
            yield _event("error", {"code": "provider_error", "partial": bool(chunks)})

    async def response() -> AsyncIterator[bytes]:
        try:
            async for event in lifecycle():
                yield event
        except asyncio.CancelledError:
            # Covers meta delivery, history loading, and provider acquisition,
            # all of which happen after the pending turn was committed.
            metadata = stream.metadata if stream is not None else ProviderMetadata()
            metadata.latency_ms = max(0, round((perf_counter() - started) * 1000))
            await _cleanup_cancelled_turn(
                session_factory,
                assistant_id=assistant.id,
                generation_id=generation.id,
                content="".join(chunks),
                locale=payload.locale,
                metadata=metadata,
            )
            raise

    return StreamingResponse(
        response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _decode_cursor(value: str | None) -> tuple[datetime, uuid.UUID] | None:
    if value is None:
        return None
    try:
        timestamp, identifier = base64.urlsafe_b64decode(value.encode()).decode().split("|", 1)
        return datetime.fromisoformat(timestamp), uuid.UUID(identifier)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid cursor") from None


@router.get("/api/admin/conversations", response_model=ChatConversationListResponse)
async def list_conversations(
    _: Admin,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    organization_id: uuid.UUID | None = None,
    member_id: uuid.UUID | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> ChatConversationListResponse:
    if not 1 <= limit <= 100:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid limit")
    statement = select(Conversation, User.email).join(User, User.id == Conversation.user_id)
    if organization_id is not None:
        statement = statement.where(Conversation.organization_id == organization_id)
    if member_id is not None:
        statement = statement.where(Conversation.user_id == member_id)
    if created_after is not None:
        statement = statement.where(Conversation.created_at >= created_after)
    if created_before is not None:
        statement = statement.where(Conversation.created_at <= created_before)
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        cursor_created_at, cursor_id = decoded
        statement = statement.where(
            or_(
                Conversation.created_at < cursor_created_at,
                and_(
                    Conversation.created_at == cursor_created_at,
                    Conversation.id < cursor_id,
                ),
            )
        )
    rows = (
        await database.execute(
            statement.order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    has_more, rows = len(rows) > limit, rows[:limit]
    items = []
    for conversation, email in rows:
        count, latest = (
            await database.execute(
                select(func.count(Message.id), func.max(Message.created_at)).where(
                    Message.conversation_id == conversation.id
                )
            )
        ).one()
        items.append(
            ChatConversationListItem(
                id=conversation.id,
                organization_id=conversation.organization_id,
                user_id=conversation.user_id,
                member_email=email,
                title=conversation.title,
                created_at=conversation.created_at,
                latest_message_at=latest,
                message_count=int(count),
            )
        )
    next_cursor = (
        base64.urlsafe_b64encode(
            f"{items[-1].created_at.isoformat()}|{items[-1].id}".encode()
        ).decode()
        if has_more and items
        else None
    )
    return ChatConversationListResponse(items=items, next_cursor=next_cursor)


@router.get(
    "/api/admin/conversations/{conversation_id}", response_model=ChatConversationDetailResponse
)
async def conversation_detail(
    conversation_id: uuid.UUID,
    request: Request,
    actor: Admin,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> ChatConversationDetailResponse:
    conversation = await database.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    messages = (
        await database.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.sequence_number)
        )
    ).all()
    generations = (
        await database.scalars(
            select(GenerationRecord)
            .join(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(GenerationRecord.created_at)
        )
    ).all()
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        organization_id=conversation.organization_id,
        action="chat.conversation_viewed",
        target_type="conversation",
        target_id=str(conversation.id),
        request_id=request.state.request_id,
    )
    await database.commit()
    return ChatConversationDetailResponse(
        id=conversation.id,
        organization_id=conversation.organization_id,
        user_id=conversation.user_id,
        messages=[
            ChatMessageResponse(
                id=x.id,
                role=x.role,
                content=x.content,
                status=x.status,
                reply_to_message_id=x.reply_to_message_id,
                created_at=x.created_at,
            )
            for x in messages
        ],
        generations=[
            ChatGenerationResponse(
                id=x.id,
                provider=x.provider,
                requested_model=x.requested_model,
                resolved_model=x.resolved_model,
                prompt_version=x.prompt_version,
                context_digest=x.context_digest,
                context_truncated=x.context_truncated,
                input_tokens=x.input_tokens,
                output_tokens=x.output_tokens,
                latency_ms=x.latency_ms,
                status=x.status,
                error_code=x.error_code,
                created_at=x.created_at,
            )
            for x in generations
        ],
    )
