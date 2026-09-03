import asyncio
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.config import Settings
from daily_insights_api.core.enums import OrganizationStatus, SystemRole, UserStatus
from daily_insights_api.core.security import (
    generate_temporary_password,
    hash_password,
    normalize_email,
)
from daily_insights_api.modules.admin.schemas import (
    AuditEventResponse,
    InternalUserCreate,
    InternalUserResponse,
    MarketPolicyUpdate,
    MemberCreate,
    MemberResponse,
    MemberUpdate,
    OrganizationCreate,
    OrganizationResponse,
    OrganizationUpdate,
    ProvisionedInternalUserResponse,
    ProvisionedMemberResponse,
    YfinanceDailyBar,
    YfinanceDailyBarsFetch,
    YfinanceDailyBarsResponse,
    YfinanceSymbolBars,
    YfinanceSymbolFailure,
)
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.audit.models import AuditEvent
from daily_insights_api.modules.data_sources.api import TRACKED_INDICES, YfinanceAdapter
from daily_insights_api.modules.identity.api import (
    AuthContext,
    require_csrf_roles,
    require_roles,
)
from daily_insights_api.modules.identity.models import User
from daily_insights_api.modules.identity.session_models import Session
from daily_insights_api.modules.markets.api import (
    MarketResponse,
    market_responses,
    refresh_index_daily_bars,
)
from daily_insights_api.modules.markets.models import Market, OrganizationMarketPolicy
from daily_insights_api.modules.tenancy.models import Membership, Organization
from daily_insights_api.web.dependencies import get_database_session

router = APIRouter(prefix="/api/admin", tags=["administration"])
AdminRead = Annotated[AuthContext, Depends(require_roles(SystemRole.ADMIN))]
AdminWrite = Annotated[AuthContext, Depends(require_csrf_roles(SystemRole.ADMIN))]
# Comfortably inside the 60s proxy_read_timeout that infra/nginx serves /api/ with.
REFRESH_DEADLINE_SECONDS = 45.0


async def _seat_count(database: AsyncSession, organization_id: uuid.UUID) -> int:
    count = await database.scalar(
        select(func.count())
        .select_from(Membership)
        .where(
            Membership.organization_id == organization_id,
            Membership.removed_at.is_(None),
        )
    )
    return int(count or 0)


async def _organization_response(
    database: AsyncSession,
    organization: Organization,
) -> OrganizationResponse:
    return OrganizationResponse(
        id=organization.id,
        name=organization.name,
        slug=organization.slug,
        seat_limit=organization.seat_limit,
        seat_count=await _seat_count(database, organization.id),
        status=organization.status,
        created_at=organization.created_at,
        updated_at=organization.updated_at,
    )


def _member_response(membership: Membership, user: User) -> MemberResponse:
    return MemberResponse(
        membership_id=membership.id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        must_change_password=user.must_change_password,
        joined_at=membership.joined_at,
    )


def _internal_user_response(user: User) -> InternalUserResponse:
    return InternalUserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        system_role=user.system_role,
        status=user.status,
        must_change_password=user.must_change_password,
    )


async def _get_organization(
    database: AsyncSession,
    organization_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Organization:
    statement = select(Organization).where(Organization.id == organization_id)
    if for_update:
        statement = statement.with_for_update()
    organization = await database.scalar(statement)
    if organization is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "organization not found")
    return organization


async def _get_active_member(
    database: AsyncSession,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[Membership, User]:
    await _get_organization(database, organization_id, for_update=True)
    row = (
        await database.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.organization_id == organization_id,
                Membership.user_id == user_id,
                Membership.removed_at.is_(None),
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    return row[0], row[1]


@router.post(
    "/organizations",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    payload: OrganizationCreate,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> OrganizationResponse:
    organization = Organization(
        name=payload.name.strip(),
        slug=payload.slug,
        seat_limit=payload.seat_limit,
    )
    database.add(organization)
    try:
        await database.flush()
    except IntegrityError as error:
        await database.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "organization slug already exists") from error
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        organization_id=organization.id,
        action="organization.created",
        target_type="organization",
        target_id=str(organization.id),
        after={
            "name": organization.name,
            "slug": organization.slug,
            "seat_limit": organization.seat_limit,
            "status": organization.status.value,
            "contract_reference": payload.contract_reference,
        },
        reason=payload.reason,
        request_id=request.state.request_id,
    )
    await database.commit()
    return await _organization_response(database, organization)


@router.post(
    "/internal-users",
    response_model=ProvisionedInternalUserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_internal_user(
    payload: InternalUserCreate,
    request: Request,
    response: Response,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> ProvisionedInternalUserResponse:
    if payload.system_role not in {SystemRole.ADMIN, SystemRole.ASSET_MANAGER}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "internal user role must be admin or asset_manager",
        )
    settings: Settings = request.app.state.settings
    assert settings.password_pepper is not None
    temporary_password = generate_temporary_password()
    user = User(
        email=normalize_email(payload.email),
        display_name=payload.display_name.strip(),
        password_hash=hash_password(
            temporary_password,
            settings.password_pepper.get_secret_value(),
        ),
        must_change_password=True,
        system_role=payload.system_role,
        status=UserStatus.ACTIVE,
    )
    database.add(user)
    try:
        await database.flush()
    except IntegrityError as error:
        await database.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "email already exists") from error
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="identity.internal_user_created",
        target_type="user",
        target_id=str(user.id),
        reason=payload.reason,
        after={"email": user.email, "system_role": user.system_role.value},
        request_id=request.state.request_id,
    )
    await database.commit()
    response.headers["Cache-Control"] = "no-store"
    user_response = _internal_user_response(user)
    return ProvisionedInternalUserResponse(
        **user_response.model_dump(),
        temporary_password=temporary_password,
    )


@router.get("/internal-users", response_model=list[InternalUserResponse])
async def list_internal_users(
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[InternalUserResponse]:
    users = (
        await database.scalars(
            select(User)
            .where(User.system_role.in_([SystemRole.ADMIN, SystemRole.ASSET_MANAGER]))
            .order_by(User.email)
        )
    ).all()
    return [_internal_user_response(user) for user in users]


@router.get("/organizations", response_model=list[OrganizationResponse])
async def list_organizations(
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[OrganizationResponse]:
    organizations = (
        await database.scalars(select(Organization).order_by(Organization.created_at))
    ).all()
    return [await _organization_response(database, organization) for organization in organizations]


@router.get("/organizations/{organization_id}", response_model=OrganizationResponse)
async def get_organization(
    organization_id: uuid.UUID,
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> OrganizationResponse:
    return await _organization_response(
        database,
        await _get_organization(database, organization_id),
    )


@router.patch("/organizations/{organization_id}", response_model=OrganizationResponse)
async def update_organization(
    organization_id: uuid.UUID,
    payload: OrganizationUpdate,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> OrganizationResponse:
    organization = await _get_organization(database, organization_id, for_update=True)
    before = {
        "name": organization.name,
        "slug": organization.slug,
        "seat_limit": organization.seat_limit,
        "status": organization.status.value,
    }
    if payload.seat_limit is not None:
        occupied = await _seat_count(database, organization.id)
        if payload.seat_limit < occupied:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"seat limit cannot be lower than occupied seats ({occupied})",
            )
        organization.seat_limit = payload.seat_limit
    if payload.name is not None:
        organization.name = payload.name.strip()
    if payload.slug is not None:
        organization.slug = payload.slug
    if payload.status is not None:
        organization.status = payload.status
        if payload.status != OrganizationStatus.ACTIVE:
            member_user_ids = select(Membership.user_id).where(
                Membership.organization_id == organization.id,
                Membership.removed_at.is_(None),
            )
            await database.execute(
                update(Session)
                .where(Session.user_id.in_(member_user_ids), Session.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )
    after = {
        "name": organization.name,
        "slug": organization.slug,
        "seat_limit": organization.seat_limit,
        "status": organization.status.value,
        "contract_reference": payload.contract_reference,
    }
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        organization_id=organization.id,
        action="organization.updated",
        target_type="organization",
        target_id=str(organization.id),
        reason=payload.reason,
        before=before,
        after=after,
        request_id=request.state.request_id,
    )
    try:
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "organization slug already exists") from error
    await database.refresh(organization)
    return await _organization_response(database, organization)


@router.delete("/organizations/{organization_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_organization(
    organization_id: uuid.UUID,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    reason: Annotated[str, Query(min_length=1, max_length=500)],
    contract_reference: Annotated[str, Query(min_length=1, max_length=200)],
) -> None:
    reason = reason.strip()
    contract_reference = contract_reference.strip()
    if not reason or not contract_reference:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "reason and contract_reference must not be blank",
        )
    organization = await _get_organization(database, organization_id, for_update=True)
    before = {"status": organization.status.value}
    organization.status = OrganizationStatus.ARCHIVED
    member_user_ids = select(Membership.user_id).where(
        Membership.organization_id == organization.id,
        Membership.removed_at.is_(None),
    )
    await database.execute(
        update(Session)
        .where(Session.user_id.in_(member_user_ids), Session.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        organization_id=organization.id,
        action="organization.archived",
        target_type="organization",
        target_id=str(organization.id),
        reason=reason,
        before=before,
        after={
            "status": organization.status.value,
            "contract_reference": contract_reference,
        },
        request_id=request.state.request_id,
    )
    await database.commit()


@router.post(
    "/organizations/{organization_id}/members",
    response_model=ProvisionedMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_member(
    organization_id: uuid.UUID,
    payload: MemberCreate,
    request: Request,
    response: Response,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> ProvisionedMemberResponse:
    organization = await _get_organization(database, organization_id, for_update=True)
    if organization.status != OrganizationStatus.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, "organization is not active")
    occupied = await _seat_count(database, organization.id)
    if occupied >= organization.seat_limit:
        raise HTTPException(status.HTTP_409_CONFLICT, "organization seat limit reached")

    email = normalize_email(payload.email)
    user = await database.scalar(select(User).where(User.email == email).with_for_update())
    membership: Membership | None = None
    if user is not None:
        if user.system_role != SystemRole.ORG_MEMBER:
            raise HTTPException(status.HTTP_409_CONFLICT, "email belongs to an internal account")
        active_membership = await database.scalar(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.removed_at.is_(None),
            )
        )
        if active_membership is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "email already has an active membership")
        membership = await database.scalar(
            select(Membership).where(
                Membership.organization_id == organization.id,
                Membership.user_id == user.id,
            )
        )

    settings: Settings = request.app.state.settings
    assert settings.password_pepper is not None
    temporary_password = generate_temporary_password()
    password_hash = hash_password(
        temporary_password,
        settings.password_pepper.get_secret_value(),
    )
    if user is None:
        user = User(
            email=email,
            display_name=payload.display_name.strip(),
            password_hash=password_hash,
            must_change_password=True,
            system_role=SystemRole.ORG_MEMBER,
            status=UserStatus.ACTIVE,
        )
        database.add(user)
        try:
            await database.flush()
        except IntegrityError as error:
            await database.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "member could not be provisioned",
            ) from error
    else:
        user.display_name = payload.display_name.strip()
        user.password_hash = password_hash
        user.must_change_password = True
        user.status = UserStatus.ACTIVE

    now = datetime.now(UTC)
    if membership is None:
        membership = Membership(
            organization_id=organization.id,
            user_id=user.id,
            joined_at=now,
        )
        database.add(membership)
    else:
        membership.joined_at = now
        membership.removed_at = None
        membership.removed_by_user_id = None
    try:
        await database.flush()
    except IntegrityError as error:
        await database.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "member could not be provisioned") from error
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        organization_id=organization.id,
        action="membership.created",
        target_type="membership",
        target_id=str(membership.id),
        reason=payload.reason,
        after={"user_id": str(user.id), "email": user.email},
        request_id=request.state.request_id,
    )
    try:
        await database.commit()
    except IntegrityError as error:
        await database.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "member could not be provisioned") from error
    response.headers["Cache-Control"] = "no-store"
    member_response = _member_response(membership, user)
    return ProvisionedMemberResponse(
        **member_response.model_dump(),
        temporary_password=temporary_password,
    )


@router.get(
    "/organizations/{organization_id}/members",
    response_model=list[MemberResponse],
)
async def list_members(
    organization_id: uuid.UUID,
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[MemberResponse]:
    await _get_organization(database, organization_id)
    rows = (
        await database.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.organization_id == organization_id,
                Membership.removed_at.is_(None),
            )
            .order_by(User.email)
        )
    ).all()
    return [_member_response(membership, user) for membership, user in rows]


@router.patch(
    "/organizations/{organization_id}/members/{user_id}",
    response_model=MemberResponse,
)
async def update_member(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: MemberUpdate,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> MemberResponse:
    membership, user = await _get_active_member(database, organization_id, user_id)
    before = {"display_name": user.display_name, "status": user.status.value}
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip()
    if payload.status is not None:
        if payload.status == UserStatus.INVITED:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "invited status is unsupported"
            )
        user.status = payload.status
        if payload.status == UserStatus.SUSPENDED:
            await database.execute(
                update(Session)
                .where(Session.user_id == user.id, Session.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        organization_id=organization_id,
        action="membership.updated",
        target_type="membership",
        target_id=str(membership.id),
        reason=payload.reason,
        before=before,
        after={"display_name": user.display_name, "status": user.status.value},
        request_id=request.state.request_id,
    )
    await database.commit()
    return _member_response(membership, user)


@router.delete(
    "/organizations/{organization_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    reason: Annotated[str, Query(min_length=1, max_length=500)],
) -> None:
    reason = reason.strip()
    if not reason:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "reason must not be blank")
    membership, user = await _get_active_member(database, organization_id, user_id)
    now = datetime.now(UTC)
    before_status = user.status.value
    membership.removed_at = now
    membership.removed_by_user_id = actor.user.id
    user.status = UserStatus.SUSPENDED
    await database.execute(
        update(Session)
        .where(Session.user_id == user.id, Session.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        organization_id=organization_id,
        action="membership.removed",
        target_type="membership",
        target_id=str(membership.id),
        reason=reason,
        before={
            "user_id": str(user.id),
            "email": user.email,
            "status": before_status,
            "removed_at": None,
        },
        after={"status": user.status.value, "removed_at": now.isoformat()},
        request_id=request.state.request_id,
    )
    await database.commit()


@router.get(
    "/organizations/{organization_id}/markets",
    response_model=list[MarketResponse],
)
async def list_organization_markets(
    organization_id: uuid.UUID,
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> list[MarketResponse]:
    await _get_organization(database, organization_id)
    return await market_responses(database, organization_id, visible_only=False)


@router.put(
    "/organizations/{organization_id}/markets/{market_code}",
    response_model=MarketResponse,
)
async def set_organization_market(
    organization_id: uuid.UUID,
    market_code: str,
    payload: MarketPolicyUpdate,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> MarketResponse:
    await _get_organization(database, organization_id, for_update=True)
    market = await database.get(Market, market_code)
    if market is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "market not found")
    policy = await database.scalar(
        select(OrganizationMarketPolicy)
        .where(
            OrganizationMarketPolicy.organization_id == organization_id,
            OrganizationMarketPolicy.market_code == market_code,
        )
        .with_for_update()
    )
    before = {
        "is_visible": policy.is_visible if policy is not None else True,
        "contract_reference": policy.contract_reference if policy is not None else None,
        "reason": policy.reason if policy is not None else None,
    }
    if payload.is_visible:
        if policy is not None:
            await database.delete(policy)
    elif policy is None:
        database.add(
            OrganizationMarketPolicy(
                organization_id=organization_id,
                market_code=market_code,
                is_visible=False,
                contract_reference=payload.contract_reference,
                reason=payload.reason,
                changed_by_user_id=actor.user.id,
                changed_at=datetime.now(UTC),
            )
        )
    else:
        policy.is_visible = False
        policy.contract_reference = payload.contract_reference
        policy.reason = payload.reason
        policy.changed_by_user_id = actor.user.id
        policy.changed_at = datetime.now(UTC)
    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        organization_id=organization_id,
        action="market_policy.updated",
        target_type="market",
        target_id=market_code,
        reason=payload.reason,
        before=before,
        after={
            "is_visible": payload.is_visible,
            "contract_reference": payload.contract_reference,
            "reason": payload.reason,
        },
        request_id=request.state.request_id,
    )
    await database.commit()
    return MarketResponse(
        code=market.code,
        name_en=market.name_en,
        name_zh_hant=market.name_zh_hant,
        name_zh_hans=market.name_zh_hans,
        is_visible=payload.is_visible,
    )


@router.post(
    "/data-sources/yfinance/daily-bars",
    response_model=YfinanceDailyBarsResponse,
)
async def fetch_yfinance_daily_bars(
    payload: YfinanceDailyBarsFetch,
    request: Request,
    actor: AdminWrite,
    database: Annotated[AsyncSession, Depends(get_database_session)],
) -> YfinanceDailyBarsResponse:
    settings: Settings = request.app.state.settings
    if not settings.yfinance_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "yfinance is not enabled")

    requested = list(TRACKED_INDICES) if payload.symbols is None else payload.symbols

    try:
        # Fail inside the proxy's 60s budget (infra/nginx/conf.d/default.conf,
        # `location ^~ /api/`). Letting nginx time out first would hand the
        # admin a 504 while this request kept fetching, writing rows and
        # recording an audit event nobody could see.
        async with asyncio.timeout(REFRESH_DEADLINE_SECONDS):
            refreshed, failures = await refresh_index_daily_bars(
                database,
                adapter=YfinanceAdapter(timeout_seconds=settings.yfinance_timeout_seconds),
                symbols=requested,
                period=payload.period,
            )
    except TimeoutError:
        # Nothing committed: the session is rolled back by its dependency.
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "yfinance refresh exceeded its budget; run "
            "daily_insights_api.scripts.run_index_daily_bars for a large backfill",
        ) from None
    succeeded = [
        YfinanceSymbolBars(
            symbol=entry.result.symbol,
            market=entry.result.market,
            as_of=entry.result.provenance.as_of,
            record_count=entry.result.provenance.record_count,
            stored_count=entry.stored_count,
            dropped_unsettled_trade_date=entry.result.dropped_unsettled_trade_date,
            bars=[
                YfinanceDailyBar(
                    trade_date=bar.trade_date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                )
                for bar in entry.result.items
            ],
        )
        for entry in refreshed
    ]
    failed = [
        YfinanceSymbolFailure(symbol=entry.symbol, market=entry.market, error=entry.error)
        for entry in failures
    ]

    record_audit_event(
        database,
        actor_user_id=actor.user.id,
        action="data_source.yfinance.fetched",
        target_type="data_source",
        target_id="yfinance",
        after={
            "period": payload.period,
            "requested": requested,
            "succeeded": [entry.symbol for entry in succeeded],
            "failed": [entry.symbol for entry in failed],
        },
        request_id=request.state.request_id,
    )
    await database.commit()
    return YfinanceDailyBarsResponse(
        period=payload.period,
        fetched_at=datetime.now(UTC),
        succeeded=succeeded,
        failed=failed,
    )


@router.get("/audit-events", response_model=list[AuditEventResponse])
async def list_audit_events(
    _: AdminRead,
    database: Annotated[AsyncSession, Depends(get_database_session)],
    organization_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[AuditEventResponse]:
    if not 1 <= limit <= 500:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "limit must be between 1 and 500"
        )
    statement = select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
    if organization_id is not None:
        statement = statement.where(AuditEvent.organization_id == organization_id)
    events = (await database.scalars(statement)).all()
    return [
        AuditEventResponse(
            id=event.id,
            actor_user_id=event.actor_user_id,
            organization_id=event.organization_id,
            action=event.action,
            target_type=event.target_type,
            target_id=event.target_id,
            reason=event.reason,
            before=event.before,
            after=event.after,
            request_id=event.request_id,
            created_at=event.created_at,
        )
        for event in events
    ]
