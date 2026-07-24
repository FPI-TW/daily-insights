import uuid
from dataclasses import dataclass
from typing import Protocol

from daily_insights_api.core.enums import OrganizationStatus


@dataclass(frozen=True)
class ActiveMembership:
    membership_id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    organization_status: OrganizationStatus


class TenancyReader(Protocol):
    async def active_membership_for_user(self, user_id: uuid.UUID) -> ActiveMembership | None: ...
