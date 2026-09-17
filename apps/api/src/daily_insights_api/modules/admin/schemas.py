import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from daily_insights_api.core.enums import OrganizationStatus, SystemRole, UserStatus


class AdminInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class OrganizationCreate(AdminInput):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=100)
    seat_limit: int = Field(ge=1)
    contract_reference: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


class OrganizationUpdate(AdminInput):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    slug: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        max_length=100,
    )
    seat_limit: int | None = Field(default=None, ge=1)
    status: OrganizationStatus | None = None
    contract_reference: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


class OrganizationResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    seat_limit: int
    seat_count: int
    status: OrganizationStatus
    created_at: datetime
    updated_at: datetime


class MemberCreate(AdminInput):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


class MemberUpdate(AdminInput):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    status: UserStatus | None = None
    reason: str = Field(min_length=1, max_length=500)


class MemberResponse(BaseModel):
    membership_id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str
    status: UserStatus
    must_change_password: bool
    joined_at: datetime


class ProvisionedMemberResponse(MemberResponse):
    temporary_password: str


class InternalUserCreate(AdminInput):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    system_role: SystemRole
    reason: str = Field(min_length=1, max_length=500)


class InternalUserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    system_role: SystemRole
    status: UserStatus
    must_change_password: bool


class ProvisionedInternalUserResponse(InternalUserResponse):
    temporary_password: str


class MarketPolicyUpdate(AdminInput):
    is_visible: bool
    contract_reference: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


class AuditEventResponse(BaseModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    organization_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: str
    reason: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request_id: str | None
    created_at: datetime
