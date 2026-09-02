from pathlib import Path

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.models import Base


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
