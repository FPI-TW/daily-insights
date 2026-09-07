"""Database-backed admission shared by every worker and conversation."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import GenerationStatus, MessageRole
from daily_insights_api.modules.chat.models import Conversation, Message
from daily_insights_api.modules.model_runtime.api import GenerationRecord

# Settings bounds every response lifetime to 600 seconds. Keep a cleanup grace
# period and use this upper bound across workers with different timeout settings.
PENDING_RECOVERY_SECONDS = 630


async def lock_admission(
    database: AsyncSession, *, user_id: uuid.UUID, organization_id: uuid.UUID
) -> None:
    for scope in (f"chat-user:{user_id}", f"chat-org:{organization_id}"):
        key = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], signed=True)
        await database.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    expired = list(
        (
            await database.scalars(
                select(Message)
                .join(Conversation)
                .where(
                    or_(
                        Conversation.user_id == user_id,
                        Conversation.organization_id == organization_id,
                    ),
                    Message.role == MessageRole.ASSISTANT,
                    Message.status == GenerationStatus.PENDING,
                    Message.created_at
                    < datetime.now(UTC) - timedelta(seconds=PENDING_RECOVERY_SECONDS),
                )
                .with_for_update(of=Message)
            )
        ).all()
    )
    for assistant in expired:
        generation = await database.scalar(
            select(GenerationRecord)
            .where(GenerationRecord.message_id == assistant.id)
            .with_for_update()
        )
        assistant.status = GenerationStatus.ERROR
        if generation is not None:
            generation.status = GenerationStatus.ERROR
            generation.error_code = "expired"
    await database.flush()


async def enforce_limits(
    database: AsyncSession, *, user_id: uuid.UUID, organization_id: uuid.UUID, settings: Settings
) -> None:
    # Admission holds both transaction locks through the new pending row commit.
    # All attempts count toward the rolling budget, including errors/cancellation.
    for scope, pending_limit, daily_limit in (
        (
            Conversation.user_id == user_id,
            settings.chat_user_max_pending,
            settings.chat_user_daily_turns,
        ),
        (
            Conversation.organization_id == organization_id,
            settings.chat_org_max_pending,
            settings.chat_org_daily_turns,
        ),
    ):
        pending, daily = (
            await database.execute(
                select(
                    func.count().filter(Message.status == GenerationStatus.PENDING),
                    func.count().filter(
                        Message.created_at >= datetime.now(UTC) - timedelta(days=1)
                    ),
                )
                .select_from(Message)
                .join(Conversation)
                .where(scope, Message.role == MessageRole.ASSISTANT)
            )
        ).one()
        if pending >= pending_limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "chat pending limit reached")
        if daily >= daily_limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "chat daily limit reached")
