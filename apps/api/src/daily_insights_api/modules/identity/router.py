from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import OrganizationStatus, SystemRole, UserStatus
from daily_insights_api.core.security import (
    PasswordPolicyError,
    dummy_password_hash,
    hash_password,
    normalize_email,
    password_needs_rehash,
    verify_password,
)
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.identity.auth import AuthContext, get_auth_context, require_csrf
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.rate_limit import (
    clear_login_attempts,
    client_ip,
    consume_login_attempt,
)
from daily_insights_api.modules.identity.schemas import (
    AuthenticationResponse,
    ChangePasswordRequest,
    CsrfTokenResponse,
    LoginRequest,
    UserResponse,
)
from daily_insights_api.modules.identity.service import create_session, rotate_csrf_token
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.modules.tenancy.models import Membership, Organization
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/auth", tags=["authentication"])


def _user_response(context: AuthContext) -> UserResponse:
    return UserResponse(
        id=context.user.id,
        email=context.user.email,
        display_name=context.user.display_name,
        system_role=context.user.system_role,
        status=context.user.status,
        must_change_password=context.user.must_change_password,
        organization_id=context.organization_id,
    )


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.environment not in {"development", "test"},
        samesite="strict",
        path="/",
    )


@router.post(
    "/login",
    response_model=AuthenticationResponse,
    operation_id="auth_login",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> AuthenticationResponse:
    settings: Settings = request.app.state.settings
    email = normalize_email(payload.email)
    remote_ip = client_ip(request, settings.trusted_proxy_cidrs)
    if not await consume_login_attempt(
        request.app.state.session_factory,
        settings=settings,
        ip_address=remote_ip,
        email=email,
    ):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts")

    user = await database.scalar(select(User).where(User.email == email))
    assert settings.password_pepper is not None
    pepper = settings.password_pepper.get_secret_value()
    candidate_hash = user.password_hash if user is not None else dummy_password_hash(pepper)
    password_is_valid = verify_password(payload.password, candidate_hash, pepper)
    organization_id = None
    organization_is_active = True
    if user is not None and user.system_role == SystemRole.ORG_MEMBER:
        organization_id = await database.scalar(
            select(Membership.organization_id).where(
                Membership.user_id == user.id,
                Membership.removed_at.is_(None),
            )
        )
        organization = (
            await database.scalar(
                select(Organization).where(Organization.id == organization_id).with_for_update()
            )
            if organization_id is not None
            else None
        )
        membership = (
            await database.scalar(
                select(Membership)
                .where(
                    Membership.organization_id == organization_id,
                    Membership.user_id == user.id,
                    Membership.removed_at.is_(None),
                )
                .with_for_update()
            )
            if organization is not None
            else None
        )
        user = await database.scalar(
            select(User)
            .where(User.id == user.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        organization_is_active = (
            organization is not None
            and membership is not None
            and user is not None
            and organization.status == OrganizationStatus.ACTIVE
        )
    elif user is not None:
        user = await database.scalar(
            select(User)
            .where(User.id == user.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    if user is not None:
        password_is_valid = verify_password(payload.password, user.password_hash, pepper)
    valid = (
        user is not None
        and user.status == UserStatus.ACTIVE
        and password_is_valid
        and organization_is_active
    )
    if not valid or user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    await clear_login_attempts(
        request.app.state.session_factory,
        settings=settings,
        ip_address=remote_ip,
        email=email,
    )
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password, pepper)
    session, token, csrf_token = create_session(database, user=user, settings=settings)
    await database.flush()
    context = AuthContext(user=user, session=session, organization_id=organization_id)
    record_audit_event(
        database,
        actor_user_id=user.id,
        organization_id=organization_id,
        action="identity.login",
        target_type="session",
        target_id=str(session.id),
        request_id=request.state.request_id,
    )
    await database.commit()
    _set_session_cookie(response, token, settings)
    response.headers["Cache-Control"] = "no-store"
    return AuthenticationResponse(user=_user_response(context), csrf_token=csrf_token)


@router.get("/me", response_model=UserResponse, operation_id="auth_get_current_user")
async def me(
    response: Response,
    context: Annotated[AuthContext, Depends(get_auth_context)],
) -> UserResponse:
    response.headers["Cache-Control"] = "no-store"
    return _user_response(context)


@router.post(
    "/csrf",
    response_model=CsrfTokenResponse,
    operation_id="auth_rotate_csrf_token",
)
async def refresh_csrf_token(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(get_auth_context)],
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> CsrfTokenResponse:
    """Rotate a CSRF token using only the authenticated same-site session cookie.

    This endpoint deliberately does not require the previous CSRF token: it is
    the recovery path after a reload. The strict SameSite session cookie and
    same-origin browser transport remain the request boundary.
    """
    origin = request.headers.get("origin")
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    forwarded_host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    expected_origin = f"{forwarded_proto}://{forwarded_host}"
    if origin != expected_origin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "same-origin request required")

    settings: Settings = request.app.state.settings
    csrf_token = rotate_csrf_token(context.session, settings)
    await database.commit()
    response.headers["Cache-Control"] = "no-store"
    return CsrfTokenResponse(csrf_token=csrf_token)


@router.post(
    "/change-password",
    response_model=AuthenticationResponse,
    operation_id="auth_change_password",
)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_csrf)],
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> AuthenticationResponse:
    settings: Settings = request.app.state.settings
    if context.organization_id is not None:
        organization = await database.scalar(
            select(Organization).where(Organization.id == context.organization_id).with_for_update()
        )
        if organization is None or organization.status != OrganizationStatus.ACTIVE:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired session")
        membership = await database.scalar(
            select(Membership)
            .where(
                Membership.organization_id == context.organization_id,
                Membership.user_id == context.user.id,
                Membership.removed_at.is_(None),
            )
            .with_for_update()
        )
        if membership is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired session")
    locked_user = await database.scalar(
        select(User)
        .where(User.id == context.user.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if locked_user is None or locked_user.status != UserStatus.ACTIVE:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired session")
    assert settings.password_pepper is not None
    pepper = settings.password_pepper.get_secret_value()
    if not verify_password(payload.current_password, locked_user.password_hash, pepper):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "current password is incorrect")
    try:
        new_password_hash = hash_password(payload.new_password, pepper)
    except PasswordPolicyError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
    if verify_password(payload.new_password, locked_user.password_hash, pepper):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "new password must be different")

    locked_user.password_hash = new_password_hash
    locked_user.must_change_password = False
    await database.execute(
        update(Session)
        .where(Session.user_id == locked_user.id, Session.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    new_session, token, csrf_token = create_session(
        database,
        user=locked_user,
        settings=settings,
    )
    await database.flush()
    record_audit_event(
        database,
        actor_user_id=locked_user.id,
        organization_id=context.organization_id,
        action="identity.password_changed",
        target_type="user",
        target_id=str(locked_user.id),
        request_id=request.state.request_id,
    )
    await database.commit()
    _set_session_cookie(response, token, settings)
    response.headers["Cache-Control"] = "no-store"
    new_context = AuthContext(
        user=locked_user,
        session=new_session,
        organization_id=context.organization_id,
    )
    return AuthenticationResponse(user=_user_response(new_context), csrf_token=csrf_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="auth_logout",
)
async def logout(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_csrf)],
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> None:
    context.session.revoked_at = datetime.now(UTC)
    record_audit_event(
        database,
        actor_user_id=context.user.id,
        organization_id=context.organization_id,
        action="identity.logout",
        target_type="session",
        target_id=str(context.session.id),
        request_id=request.state.request_id,
    )
    await database.commit()
    settings: Settings = request.app.state.settings
    response.delete_cookie(settings.session_cookie_name, path="/")
