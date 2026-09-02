import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from daily_insights_api.core.enums import GenerationStatus, MessageRole
from daily_insights_api.modules.reports.api import Locale


class ReportsIndexContext(BaseModel):
    kind: Literal["reports_index"]
    publication_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    news_edition_id: uuid.UUID | None = None


class ReportDetailContext(BaseModel):
    kind: Literal["report_detail"]
    publication_id: uuid.UUID


PageContext = Annotated[ReportsIndexContext | ReportDetailContext, Field(discriminator="kind")]


class ChatStreamRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    conversation_id: uuid.UUID | None = None
    client_request_id: uuid.UUID
    locale: Locale
    message: str = Field(min_length=1, max_length=4000)
    page_context: PageContext


class ChatMessageResponse(BaseModel):
    id: uuid.UUID
    role: MessageRole
    content: str
    status: GenerationStatus
    reply_to_message_id: uuid.UUID | None
    created_at: datetime


class ChatConversationListItem(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    member_email: str
    title: str | None
    created_at: datetime
    latest_message_at: datetime | None
    message_count: int


class ChatConversationListResponse(BaseModel):
    items: list[ChatConversationListItem]
    next_cursor: str | None


class ChatGenerationResponse(BaseModel):
    id: uuid.UUID
    provider: str
    requested_model: str
    resolved_model: str | None
    prompt_version: str
    context_digest: str | None
    context_truncated: bool
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    status: GenerationStatus
    error_code: str | None
    created_at: datetime


class ChatConversationDetailResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    messages: list[ChatMessageResponse]
    generations: list[ChatGenerationResponse]
