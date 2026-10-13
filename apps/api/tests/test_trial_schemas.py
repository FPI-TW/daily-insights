from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from daily_insights_api.modules.admin.schemas import MemberCreate, MemberUpdate
from daily_insights_api.modules.tenancy.models import Membership


@pytest.mark.parametrize("days", [0, -1, 1.5, True, "7"])
def test_trial_days_must_be_positive_integer(days: object) -> None:
    with pytest.raises(ValidationError):
        MemberCreate.model_validate(
            {
                "email": "a@example.com",
                "display_name": "A",
                "reason": "test",
                "member_type": "trial",
                "trial_days": days,
            }
        )


def test_regular_members_reject_trial_days_and_updates_require_timezone() -> None:
    with pytest.raises(ValidationError):
        MemberCreate(email="a@example.com", display_name="A", reason="test", trial_days=7)
    invalid_fields: list[dict[str, object]] = [
        {"trial_expires_at": "2026-11-01T00:00:00"},
        {"trial_expires_at": None},
        {"member_type": None},
        {"member_type": "regular", "trial_expires_at": "2026-11-01T00:00:00Z"},
    ]
    for fields in invalid_fields:
        with pytest.raises(ValidationError):
            MemberUpdate.model_validate({"reason": "test", **fields})


def test_trial_expiry_boundary() -> None:
    now = datetime.now(UTC)
    membership = Membership(member_type="trial", trial_expires_at=now)
    assert not membership.trial_expired(now - timedelta(microseconds=1))
    assert membership.trial_expired(now)
    assert membership.trial_expired(now + timedelta(microseconds=1))
