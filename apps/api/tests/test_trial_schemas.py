from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from daily_insights_api.modules.admin.schemas import InternalUserCreate, MemberCreate, MemberUpdate
from daily_insights_api.modules.tenancy.models import Membership


@pytest.mark.parametrize("days", [0, -1, 1.5, True, "7"])
def test_trial_days_must_be_positive_integer(days: object) -> None:
    with pytest.raises(ValidationError):
        MemberCreate.model_validate(
            {
                "email": "a@example.com",
                "display_name": "A",
                "password": "Abcd1234",
                "reason": "test",
                "member_type": "trial",
                "trial_days": days,
            }
        )


def test_regular_members_reject_trial_days_and_updates_require_timezone() -> None:
    with pytest.raises(ValidationError):
        MemberCreate(
            email="a@example.com",
            display_name="A",
            password="Abcd1234",
            reason="test",
            trial_days=7,
        )
    invalid_fields: list[dict[str, object]] = [
        {"trial_expires_at": "2026-11-01T00:00:00"},
        {"trial_expires_at": None},
        {"member_type": None},
        {"member_type": "regular", "trial_expires_at": "2026-11-01T00:00:00Z"},
    ]
    for fields in invalid_fields:
        with pytest.raises(ValidationError):
            MemberUpdate.model_validate({"reason": "test", **fields})


@pytest.mark.parametrize(
    "password", ["short7", "Invalid!123", "Password 123", "密碼123456", " Abcd1234 "]
)
@pytest.mark.parametrize("account_type", [MemberCreate, InternalUserCreate])
def test_account_creation_rejects_invalid_passwords(
    account_type: type[MemberCreate] | type[InternalUserCreate], password: str
) -> None:
    payload: dict[str, object] = {
        "email": "a@example.com",
        "display_name": "A",
        "password": password,
        "reason": "test",
    }
    if account_type is InternalUserCreate:
        payload["system_role"] = "asset_manager"
    with pytest.raises(ValidationError):
        account_type.model_validate(payload)


@pytest.mark.parametrize("password", ["abcdefgh", "ABCDEFGH", "12345678", "Abcd1234"])
@pytest.mark.parametrize("account_type", [MemberCreate, InternalUserCreate])
def test_account_creation_accepts_eight_or_more_letters_and_digits(
    account_type: type[MemberCreate] | type[InternalUserCreate], password: str
) -> None:
    payload: dict[str, object] = {
        "email": "a@example.com",
        "display_name": "A",
        "password": password,
        "reason": "test",
    }
    if account_type is InternalUserCreate:
        payload["system_role"] = "asset_manager"
    assert account_type.model_validate(payload).password == password


def test_trial_expiry_boundary() -> None:
    now = datetime.now(UTC)
    membership = Membership(member_type="trial", trial_expires_at=now)
    assert not membership.trial_expired(now - timedelta(microseconds=1))
    assert membership.trial_expired(now)
    assert membership.trial_expired(now + timedelta(microseconds=1))
