import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from conftest import reset_database_schema
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from daily_insights_api.core.config import get_settings

pytestmark = pytest.mark.integration


def test_trial_migration_preserves_regular_members_and_enforces_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required")
    monkeypatch.setenv("DAILY_INSIGHTS_DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    engine = create_engine(url)
    reset_database_schema(url)
    try:
        command.upgrade(config, "20261007_0032")
        ids = {"org": uuid.uuid4(), "user": uuid.uuid4(), "membership": uuid.uuid4()}
        with engine.begin() as database:
            database.execute(
                text(
                    "INSERT INTO organizations(id,name,slug,seat_limit) "
                    "VALUES (:org,'Migration','migration',1)"
                ),
                ids,
            )
            database.execute(
                text(
                    "INSERT INTO users(id,email,display_name,password_hash) "
                    "VALUES (:user,'migration@example.com','Migration','test-only-hash')"
                ),
                ids,
            )
            database.execute(
                text(
                    "INSERT INTO memberships(id,organization_id,user_id) "
                    "VALUES (:membership,:org,:user)"
                ),
                ids,
            )
        command.upgrade(config, "head")
        with engine.connect() as database:
            row = database.execute(
                text("SELECT member_type,trial_expires_at FROM memberships")
            ).one()
            assert row == ("regular", None)
        for sql in [
            "UPDATE memberships SET member_type='trial'",
            "UPDATE memberships SET trial_expires_at=now()",
            "UPDATE memberships SET member_type='unknown'",
        ]:
            with pytest.raises(IntegrityError), engine.begin() as database:
                database.execute(text(sql))
        command.downgrade(config, "20261007_0032")
        command.upgrade(config, "head")
        with engine.connect() as database:
            assert database.scalar(text("SELECT count(*) FROM memberships")) == 1
    finally:
        engine.dispose()
        reset_database_schema(url)
        get_settings.cache_clear()
