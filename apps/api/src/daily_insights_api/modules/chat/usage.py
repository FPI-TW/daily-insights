"""Aggregate reported token counts without treating missing provider usage as zero."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.chat.models import Message
from daily_insights_api.modules.chat.schemas import TokenUsageSummary
from daily_insights_api.modules.model_runtime.api import GenerationRecord


def token_usage_summary(
    input_tokens: int | None,
    output_tokens: int | None,
    generation_count: int,
    input_count: int,
    output_count: int,
) -> TokenUsageSummary:
    known = input_count + output_count
    return TokenUsageSummary(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=(input_tokens or 0) + (output_tokens or 0) if known else None,
        completeness="unavailable"
        if not known
        else ("complete" if known == generation_count * 2 else "partial"),
    )


def generation_usage(generations: Sequence[GenerationRecord]) -> TokenUsageSummary:
    """Use the same snapshot of generations as the detail response."""
    inputs = [row.input_tokens for row in generations if row.input_tokens is not None]
    outputs = [row.output_tokens for row in generations if row.output_tokens is not None]
    return token_usage_summary(
        sum(inputs) if inputs else None,
        sum(outputs) if outputs else None,
        len(generations),
        len(inputs),
        len(outputs),
    )


async def conversation_usage(
    database: AsyncSession, conversation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, TokenUsageSummary]:
    if not conversation_ids:
        return {}
    rows = (
        await database.execute(
            select(
                Message.conversation_id,
                func.sum(GenerationRecord.input_tokens),
                func.sum(GenerationRecord.output_tokens),
                func.count(GenerationRecord.id),
                func.count(GenerationRecord.input_tokens),
                func.count(GenerationRecord.output_tokens),
            )
            .join(GenerationRecord, GenerationRecord.message_id == Message.id)
            .where(Message.conversation_id.in_(conversation_ids))
            .group_by(Message.conversation_id)
        )
    ).all()
    summaries = {row[0]: token_usage_summary(*row[1:]) for row in rows}
    return {
        id: summaries.get(id, token_usage_summary(None, None, 0, 0, 0)) for id in conversation_ids
    }
