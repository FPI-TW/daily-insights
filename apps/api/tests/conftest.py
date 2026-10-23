import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic_settings import BaseSettings
from sqlalchemy import URL, make_url, text
from sqlalchemy import create_engine as create_sync_engine

from daily_insights_api.core import config as config_module
from daily_insights_api.core import security
from daily_insights_api.core.config import get_settings

TEST_DATABASE_URL_ENV = "DAILY_INSIGHTS_TEST_DATABASE_URL"
# CI sets this so a renamed or missing database URL fails the run instead of
# silently skipping every integration test.
TEST_DATABASE_REQUIRED_ENV = "DAILY_INSIGHTS_TEST_DATABASE_REQUIRED"
# Integration fixtures drop schemas and whole databases, so the configured URL
# must name an obviously disposable database such as `daily_insights_test`.
DISPOSABLE_DATABASE_NAME = re.compile(r"(^|_)(test|ci)(_|$)")


@dataclass(frozen=True)
class _DerivedDatabases:
    maintenance: URL
    working: URL
    template: URL


# Set by `isolated_test_database` once the per-process template is migrated.
_derived_databases: _DerivedDatabases | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Run every test against the same configuration CI uses.

    Settings otherwise read the developer's `apps/api/.env` and exported
    `DAILY_INSIGHTS_*` values, which can enable live providers, change
    behaviour between machines, or point a fixture at a real database.
    """
    for name in list(os.environ):
        if name.startswith("DAILY_INSIGHTS_") and not name.startswith("DAILY_INSIGHTS_TEST_"):
            del os.environ[name]
    os.environ["DAILY_INSIGHTS_ENVIRONMENT"] = "test"
    for settings_class in vars(config_module).values():
        if (
            isinstance(settings_class, type)
            and issubclass(settings_class, BaseSettings)
            and settings_class is not BaseSettings
        ):
            settings_class.model_config["env_file"] = None
    get_settings.cache_clear()

    database_url = os.environ.get(TEST_DATABASE_URL_ENV)
    if database_url is None:
        if os.environ.get(TEST_DATABASE_REQUIRED_ENV) == "true":
            raise pytest.UsageError(f"{TEST_DATABASE_URL_ENV} is required for this run")
        return
    database_name = make_url(database_url).database or ""
    if not DISPOSABLE_DATABASE_NAME.search(database_name):
        raise pytest.UsageError(
            f"{TEST_DATABASE_URL_ENV} must name a disposable database containing "
            f"'test' or 'ci' (got {database_name!r}); integration tests drop its contents"
        )


def _execute_maintenance(maintenance_url: URL, *statements: str) -> None:
    engine = create_sync_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def _recreate_database_statements(name: str, template: str | None = None) -> tuple[str, str]:
    source = f' TEMPLATE "{template}"' if template else ""
    return (
        f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)',
        f'CREATE DATABASE "{name}"{source}',
    )


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database() -> Iterator[None]:
    """Give each pytest process its own databases derived from the configured URL.

    The configured database is only used as the maintenance connection, so it
    is never modified. Each process (or pytest-xdist worker) gets a working
    database plus a template migrated to head once, which `remigrate_database`
    clones instead of replaying every migration for each test.
    """
    global _derived_databases
    configured_url = os.environ.get(TEST_DATABASE_URL_ENV)
    if configured_url is None:
        yield
        return

    maintenance_url = make_url(configured_url)
    suffix = os.environ.get("PYTEST_XDIST_WORKER", "main")
    working_url = maintenance_url.set(database=f"{maintenance_url.database}_{suffix}")
    template_url = maintenance_url.set(database=f"{maintenance_url.database}_{suffix}_template")
    assert working_url.database is not None
    assert template_url.database is not None

    _execute_maintenance(
        maintenance_url,
        *_recreate_database_statements(template_url.database),
        *_recreate_database_statements(working_url.database),
    )
    _migrate_from_empty_schema(template_url.render_as_string(hide_password=False))
    _derived_databases = _DerivedDatabases(maintenance_url, working_url, template_url)
    os.environ[TEST_DATABASE_URL_ENV] = working_url.render_as_string(hide_password=False)
    try:
        yield
    finally:
        os.environ[TEST_DATABASE_URL_ENV] = configured_url
        _derived_databases = None
        _execute_maintenance(
            maintenance_url,
            f'DROP DATABASE IF EXISTS "{working_url.database}" WITH (FORCE)',
            f'DROP DATABASE IF EXISTS "{template_url.database}" WITH (FORCE)',
        )


def reset_database_schema(database_url: str) -> None:
    """Drop and recreate the integration database's public schema."""
    engine = create_sync_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def _migrate_from_empty_schema(database_url: str) -> None:
    """Reset the public schema and replay every migration up to head.

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


def remigrate_database(database_url: str) -> None:
    """Rebuild the integration database from an empty schema up to head.

    The per-process working database is recreated from the migrated template,
    which is equivalent to replaying the migrations but an order of magnitude
    faster; any other URL falls back to running the migrations.
    """
    databases = _derived_databases
    if databases is None or make_url(database_url).database != databases.working.database:
        _migrate_from_empty_schema(database_url)
        return
    assert databases.working.database is not None
    assert databases.template.database is not None
    _execute_maintenance(
        databases.maintenance,
        *_recreate_database_statements(databases.working.database, databases.template.database),
    )


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
