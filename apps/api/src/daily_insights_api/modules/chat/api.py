import uuid
from dataclasses import dataclass
from typing import Protocol

from daily_insights_api.core.enums import GenerationStatus, MessageRole


@dataclass(frozen=True)
class StoredMessage:
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    sequence_number: int
    role: MessageRole
    content: str
    status: GenerationStatus


class ConversationWriter(Protocol):
    async def append_user_message(
        self,
        *,
        conversation_id: uuid.UUID,
        content: str,
        idempotency_key: str,
    ) -> StoredMessage: ...
