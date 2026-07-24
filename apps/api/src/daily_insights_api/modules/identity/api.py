import uuid
from dataclasses import dataclass
from typing import Protocol

from daily_insights_api.core.enums import SystemRole, UserStatus
from daily_insights_api.modules.identity.auth import (
    AuthContext,
    get_auth_context,
    require_csrf,
    require_csrf_roles,
    require_password_changed,
    require_roles,
)


@dataclass(frozen=True)
class IdentityPrincipal:
    user_id: uuid.UUID
    email: str
    display_name: str
    role: SystemRole
    status: UserStatus
    must_change_password: bool


class IdentityReader(Protocol):
    async def get_principal(self, user_id: uuid.UUID) -> IdentityPrincipal | None: ...


__all__ = [
    "AuthContext",
    "IdentityPrincipal",
    "IdentityReader",
    "get_auth_context",
    "require_csrf",
    "require_csrf_roles",
    "require_password_changed",
    "require_roles",
]
