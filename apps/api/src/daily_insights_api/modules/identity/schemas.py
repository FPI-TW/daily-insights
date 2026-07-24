import uuid

from pydantic import BaseModel, Field

from daily_insights_api.core.enums import SystemRole, UserStatus


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    system_role: SystemRole
    status: UserStatus
    must_change_password: bool
    organization_id: uuid.UUID | None


class AuthenticationResponse(BaseModel):
    user: UserResponse
    csrf_token: str


class CsrfTokenResponse(BaseModel):
    csrf_token: str
