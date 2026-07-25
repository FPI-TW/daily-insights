import pytest
from starlette.requests import Request

from daily_insights_api.core.security import (
    PasswordPolicyError,
    generate_temporary_password,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from daily_insights_api.modules.identity.rate_limit import client_ip


def test_password_hash_is_salted_and_verifiable() -> None:
    password = "CorrectHorse123!"
    first = hash_password(password, "pepper")
    second = hash_password(password, "pepper")

    assert first != second
    assert verify_password(password, first, "pepper")
    assert not verify_password("WrongPassword123!", first, "pepper")
    assert not verify_password(password, first, "different-pepper")
    assert first.split("$")[3] == "5"
    assert not password_needs_rehash(first)
    assert password_needs_rehash(first.replace("$5$", "$1$", 1))


def test_generated_temporary_password_satisfies_policy() -> None:
    temporary_password = generate_temporary_password()

    assert len(temporary_password) == 24
    hash_password(temporary_password, "pepper")


def test_password_policy_accepts_eight_letters_without_digit_or_symbol() -> None:
    encoded = hash_password("Abcdefgh", "pepper")

    assert verify_password("Abcdefgh", encoded, "pepper")


@pytest.mark.parametrize(
    "password",
    [
        "Short1!",
        "alllowercase123!",
        "ALLUPPERCASE123!",
    ],
)
def test_password_policy_rejects_weak_passwords(password: str) -> None:
    with pytest.raises(PasswordPolicyError):
        hash_password(password, "pepper")


def test_client_ip_only_trusts_forwarded_address_from_configured_proxy() -> None:
    trusted = Request(
        {
            "type": "http",
            "client": ("127.0.0.1", 1234),
            "headers": [(b"x-real-ip", b"203.0.113.10")],
        }
    )
    untrusted = Request(
        {
            "type": "http",
            "client": ("198.51.100.20", 1234),
            "headers": [(b"x-real-ip", b"203.0.113.10")],
        }
    )

    assert client_ip(trusted, "127.0.0.0/8") == "203.0.113.10"
    assert client_ip(untrusted, "127.0.0.0/8") == "198.51.100.20"
