import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.modules.audit.models import AuditEvent


def record_audit_event(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: str,
    organization_id: uuid.UUID | None = None,
    reason: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_user_id=actor_user_id,
        organization_id=organization_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        before=before,
        after=after,
        request_id=request_id,
    )
    session.add(event)
    return event
