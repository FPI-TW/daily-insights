import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import OrganizationStatus, SystemRole, UserStatus
from daily_insights_api.core.security import hash_token
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.modules.tenancy.models import Membership, Organization
from daily_insights_api.web.dependencies import get_database_session


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: Session
    organization_id: uuid.UUID | None


async def get_auth_context(
    request: Request,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> AuthContext:
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    assert settings.session_secret is not None
    token_hash = hash_token(token, settings.session_secret.get_secret_value())
    result = await database.execute(
        select(User, Session, Membership.organization_id)
        .join(Session, Session.user_id == User.id)
        .outerjoin(
            Membership,
            and_(Membership.user_id == User.id, Membership.removed_at.is_(None)),
        )
        .outerjoin(Organization, Organization.id == Membership.organization_id)
        .where(
            Session.token_hash == token_hash,
            Session.revoked_at.is_(None),
            Session.expires_at > datetime.now(UTC),
            User.status == UserStatus.ACTIVE,
            or_(
                Membership.organization_id.is_(None),
                Organization.status == OrganizationStatus.ACTIVE,
            ),
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired session")
    user, session, organization_id = row
    return AuthContext(user=user, session=session, organization_id=organization_id)


def require_password_changed(
    context: Annotated[AuthContext, Depends(get_auth_context)],
) -> AuthContext:
    if context.user.must_change_password:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "password change required",
            headers={"X-Password-Change-Required": "true"},
        )
    return context


def require_roles(*roles: SystemRole) -> object:
    def dependency(
        context: Annotated[AuthContext, Depends(require_password_changed)],
    ) -> AuthContext:
        if context.user.system_role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient permission")
        return context

    return dependency


def require_csrf_roles(*roles: SystemRole) -> object:
    def dependency(
        context: Annotated[AuthContext, Depends(require_csrf)],
    ) -> AuthContext:
        if context.user.must_change_password:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "password change required",
                headers={"X-Password-Change-Required": "true"},
            )
        if context.user.system_role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient permission")
        return context

    return dependency


def require_csrf(
    request: Request,
    context: Annotated[AuthContext, Depends(get_auth_context)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> AuthContext:
    if csrf_token is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token required")
    settings: Settings = request.app.state.settings
    assert settings.session_secret is not None
    candidate_hash = hash_token(csrf_token, settings.session_secret.get_secret_value())
    if not hmac.compare_digest(candidate_hash, context.session.csrf_token_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")
    return context
