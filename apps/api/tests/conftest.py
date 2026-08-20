from collections.abc import Iterator

import pytest

from daily_insights_api.core import security

# Integration tests exercise authentication flows, but they are not password-
# security tests. Keep the production parameters in ``security`` untouched for
# all other tests while making the repeated integration setup and login calls
# inexpensive.
TEST_SCRYPT_N = 2**10
TEST_SCRYPT_R = 8
TEST_SCRYPT_P = 1


@pytest.fixture(autouse=True)
def cheap_scrypt_for_integration_tests(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    if request.node.get_closest_marker("integration") is None:
        yield
        return

    # The dummy hash is cached by pepper. Clear it on both sides of the
    # temporary parameter override so a hash created under one profile cannot
    # leak into another test or be reused after production constants return.
    security.dummy_password_hash.cache_clear()
    monkeypatch.setattr(security, "SCRYPT_N", TEST_SCRYPT_N)
    monkeypatch.setattr(security, "SCRYPT_R", TEST_SCRYPT_R)
    monkeypatch.setattr(security, "SCRYPT_P", TEST_SCRYPT_P)
    try:
        yield
    finally:
        security.dummy_password_hash.cache_clear()
