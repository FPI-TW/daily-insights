import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from conftest import remigrate_database
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from daily_insights_api.core.config import get_settings

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).parents[1] / "alembic.ini"
SHA = "a" * 64


@pytest.fixture
def migrated_engine() -> Iterator[tuple[str, Engine]]:
    database_url = os.getenv("DAILY_INSIGHTS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DAILY_INSIGHTS_TEST_DATABASE_URL is required for integration tests")
    remigrate_database(database_url)
    engine = create_engine(database_url)
    try:
        yield database_url, engine
    finally:
        engine.dispose()


def _alembic(database_url: str, action: str, revision: str) -> None:
    previous = os.environ.get("DAILY_INSIGHTS_DATABASE_URL")
    os.environ["DAILY_INSIGHTS_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    try:
        getattr(command, action)(Config(str(ALEMBIC_INI)), revision)
    finally:
        if previous is None:
            os.environ.pop("DAILY_INSIGHTS_DATABASE_URL", None)
        else:
            os.environ["DAILY_INSIGHTS_DATABASE_URL"] = previous
        get_settings.cache_clear()


def _insert_candidate(engine: Engine, candidate_id: str, **extra: object) -> None:
    columns = ", ".join(["candidate_id", *extra])
    values = ", ".join([":candidate_id", *(f":{name}" for name in extra)])
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO news_candidates "
                "(id, edition_id, source_name, hostname, url, headline, stage, "
                f"{columns}) "
                "SELECT gen_random_uuid(), id, 'Source', 'example.com', "
                f"'https://example.com/' || :candidate_id, 'Headline', 'unused', {values} "
                "FROM news_editions LIMIT 1"
            ),
            {"candidate_id": candidate_id, **extra},
        )


def test_overnight_schema_round_trips_and_maps_screened_out_back_to_unused(
    migrated_engine: tuple[str, Engine],
) -> None:
    database_url, engine = migrated_engine
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO news_collected_candidates "
                "(candidate_id, url, hostname, source_name, headline, seen_at, markets, "
                "source_key, first_collected_at, last_collected_at) VALUES "
                "(:sha, 'https://example.com/a', 'example.com', 'Example', 'Headline', NULL, "
                '\'["global","tw_equity"]\'::jsonb, :sha, now(), now())'
            ),
            {"sha": SHA},
        )
        connection.execute(
            text(
                "INSERT INTO news_feed_poll_states (source_key, feed_url) "
                "VALUES (:sha, 'https://example.com/feed')"
            ),
            {"sha": SHA},
        )
        poll_state = connection.execute(
            text("SELECT consecutive_failures, gap_count FROM news_feed_poll_states")
        ).one()
        assert tuple(poll_state) == (0, 0)
        connection.execute(
            text(
                "INSERT INTO news_editions (id, edition_date, market_code, revision, "
                "input_digest, derivation_version, prompt_version, status) VALUES "
                "(gen_random_uuid(), DATE '2026-09-30', 'global', 1, :sha, 't', 't', 'complete')"
            ),
            {"sha": SHA},
        )

    _insert_candidate(engine, "b" * 64, discovered_via="both", screen_rank=1, screen_score=5)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE news_candidates SET stage = 'screened_out' WHERE candidate_id = :id"),
            {"id": "b" * 64},
        )
    for invalid in ({"discovered_via": "rss"}, {"screen_score": 6}, {"screen_score": 0}):
        with pytest.raises(IntegrityError):
            _insert_candidate(engine, "c" * 64, **invalid)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO news_collected_candidates "
                "(candidate_id, url, hostname, source_name, headline, markets, source_key, "
                "first_collected_at, last_collected_at) VALUES "
                "(:sha, 'u', 'h', 's', 'h', '{}'::jsonb, :sha, now(), now())"
            ),
            {"sha": "d" * 64},
        )

    _alembic(database_url, "downgrade", "20260924_0030")
    tables = set(inspect(engine).get_table_names())
    assert not {"news_collected_candidates", "news_feed_poll_states"} & tables
    candidate_columns = {
        column["name"] for column in inspect(engine).get_columns("news_candidates")
    }
    assert not {"discovered_via", "screen_rank", "screen_score"} & candidate_columns
    with engine.begin() as connection:
        stage = connection.execute(
            text("SELECT stage FROM news_candidates WHERE candidate_id = :id"), {"id": "b" * 64}
        ).scalar_one()
        assert stage == "unused"
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(text("UPDATE news_candidates SET stage = 'screened_out'"))

    _alembic(database_url, "upgrade", "head")
    tables = set(inspect(engine).get_table_names())
    assert {"news_collected_candidates", "news_feed_poll_states"} <= tables
