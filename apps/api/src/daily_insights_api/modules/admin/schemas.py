import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from daily_insights_api.core.enums import OrganizationStatus, SystemRole, UserStatus
from daily_insights_api.modules.data_sources.api import IndexSymbol, MarketCode


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


class YfinanceDailyBarsFetch(AdminInput):
    # Typed rather than checked in the router, so the reviewed index set reaches
    # OpenAPI and the generated client as an enum and an arbitrary string can
    # never reach the scraping client.
    symbols: list[IndexSymbol] | None = Field(default=None, min_length=1, max_length=50)
    period: Literal[
        "1d", "5d", "7d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"
    ] = "2y"
    # No `reason` here on purpose: this endpoint reads an external source and
    # changes no business state, unlike the organization and market-policy
    # writes above. The audit event still records who fetched what.


class YfinanceSymbolBars(BaseModel):
    """What one symbol's refresh did, not the rows themselves.

    The bars live in index_daily_bars; repeating them here cost 717KB for a 2y
    run and would be several megabytes for `max`, and a reader wants a symbol
    and a date range, not whatever one refresh happened to touch.
    """

    symbol: str
    market: MarketCode
    as_of: date
    # Every settled bar fetched is written, so one count says both.
    stored_count: int
    # Yahoo's current still-open session bar, seen and excluded from the stored
    # series.
    dropped_unsettled_trade_date: date | None


class YfinanceSymbolFailure(BaseModel):
    symbol: str
    market: MarketCode
    error: str


class YfinanceDailyBarsResponse(BaseModel):
    period: str
    fetched_at: datetime
    succeeded: list[YfinanceSymbolBars]
    failed: list[YfinanceSymbolFailure]


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
