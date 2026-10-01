from pathlib import Path

from sqlalchemy.schema import DefaultClause

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.models import Base
from daily_insights_api.modules.news.models import CANDIDATE_STAGES


def _checks(table_name: str) -> set[str]:
    return {
        str(constraint.sqltext)
        for constraint in Base.metadata.tables[table_name].constraints
        if hasattr(constraint, "sqltext")
    }


def test_news_constraint_columns_belong_to_their_orm_tables() -> None:
    edition_checks = _checks("news_editions")
    item_checks = _checks("news_items")
    assert "rank > 0" not in edition_checks
    assert (
        "topic IN ('markets','economy','companies','policy','technology','commodities')"
        not in edition_checks
    )
    assert "rank > 0" in item_checks
    assert (
        "topic IN ('markets','economy','companies','policy','technology','commodities')"
        in item_checks
    )


def test_news_migration_matches_critical_orm_columns_and_constraints() -> None:
    migration = (
        Path(__file__).parents[1] / "migrations/versions/20260901_0007_daily_news.py"
    ).read_text()
    for table, columns in {
        "news_editions": {"edition_date", "revision", "input_digest", "generated_at", "caveat"},
        "news_items": {"rank", "topic", "source_url", "source_published_at", "content_digest"},
        "news_presentations": {"locale", "headline", "summary"},
    }.items():
        assert table in migration
        assert columns <= set(Base.metadata.tables[table].columns.keys())
        assert all(f'"{column}"' in migration for column in columns)
    assert "rank_positive" in migration and "topic_valid" in migration
    market_migration = (
        Path(__file__).parents[1] / "migrations/versions/20260902_0009_market_news_editions.py"
    ).read_text()
    assert "market_code" in Base.metadata.tables["news_editions"].columns
    assert '"market_code"' in market_migration and "market_code_valid" in market_migration
    assert "market_code IN ('global','tw_equity','us_equity')" in _checks("news_editions")


def test_migration_places_item_constraints_inside_news_items_create_table() -> None:
    migration = (
        Path(__file__).parents[1] / "migrations/versions/20260901_0007_daily_news.py"
    ).read_text()
    edition_start = migration.index('"news_editions"')
    item_start = migration.index('"news_items"')
    presentation_start = migration.index('"news_presentations"')
    edition_block = migration[edition_start:item_start]
    item_block = migration[item_start:presentation_start]
    assert "rank_positive" not in edition_block and "topic_valid" not in edition_block
    assert "rank_positive" in item_block and "topic_valid" in item_block


def test_overnight_collection_tables_match_the_shared_contract() -> None:
    collected = Base.metadata.tables["news_collected_candidates"]
    assert tuple(collected.primary_key.columns.keys()) == ("candidate_id",)
    assert set(collected.columns.keys()) == {
        "candidate_id",
        "url",
        "hostname",
        "source_name",
        "headline",
        "seen_at",
        "markets",
        "source_key",
        "first_collected_at",
        "last_collected_at",
    }
    assert [name for name, column in collected.columns.items() if column.nullable] == ["seen_at"]
    assert {index.name for index in collected.indexes} == {
        "ix_news_collected_candidates_first_collected_at",
        "ix_news_collected_candidates_last_collected_at",
    }
    assert "jsonb_typeof(markets) = 'array'" in _checks("news_collected_candidates")

    poll = Base.metadata.tables["news_feed_poll_states"]
    assert tuple(poll.primary_key.columns.keys()) == ("source_key",)
    assert set(poll.columns.keys()) == {
        "source_key",
        "feed_url",
        "last_attempt_at",
        "last_success_at",
        "last_status",
        "last_count",
        "last_error_code",
        "etag",
        "last_modified",
        "cooldown_until",
        "consecutive_failures",
        "last_gap_minutes",
        "gap_count_since",
        "gap_count",
    }
    required = {name for name, column in poll.columns.items() if not column.nullable}
    assert required == {"source_key", "feed_url", "consecutive_failures", "gap_count"}
    for counter in ("consecutive_failures", "gap_count"):
        default = poll.columns[counter].server_default
        assert isinstance(default, DefaultClause) and str(default.arg) == "0"


def test_news_candidates_accept_screening_and_discovery_markers() -> None:
    columns = Base.metadata.tables["news_candidates"].columns
    assert {"discovered_via", "screen_rank", "screen_score"} <= set(columns.keys())
    assert all(columns[name].nullable for name in ("discovered_via", "screen_rank", "screen_score"))
    checks = _checks("news_candidates")
    assert "discovered_via IS NULL OR discovered_via IN ('live','collected','both')" in checks
    assert "screen_score IS NULL OR screen_score BETWEEN 1 AND 5" in checks
    stage_check = next(check for check in checks if check.startswith("stage IN"))
    assert "'screened_out'" in stage_check
    assert all(f"'{stage}'" in stage_check for stage in CANDIDATE_STAGES)


def test_overnight_migration_follows_podcast_uploads_and_restores_previous_stages() -> None:
    migration = (
        Path(__file__).parents[1] / "migrations/versions/20260930_0031_overnight_news_collection.py"
    ).read_text()
    assert 'revision: str = "20260930_0031"' in migration
    assert 'down_revision: str | None = "20260924_0030"' in migration
    assert "SET stage = 'unused' WHERE stage = 'screened_out'" in migration
