import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text

from daily_insights_api.core import security
from daily_insights_api.core.config import get_settings


def reset_database_schema(database_url: str) -> None:
    """Drop and recreate the integration database's public schema."""
    engine = create_sync_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def remigrate_database(database_url: str) -> None:
    """Rebuild the integration database from an empty schema up to head.

    Alembic reads the URL from the environment, so it is set for the duration of
    the upgrade and restored afterwards along with the settings cache.
    """
    previous = os.environ.get("DAILY_INSIGHTS_DATABASE_URL")
    os.environ["DAILY_INSIGHTS_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        reset_database_schema(database_url)
        command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("DAILY_INSIGHTS_DATABASE_URL", None)
        else:
            os.environ["DAILY_INSIGHTS_DATABASE_URL"] = previous
        get_settings.cache_clear()


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
