import pytest
from pydantic import ValidationError

from daily_insights_api import models as registered_models  # noqa: F401
from daily_insights_api.core.models import Base
from daily_insights_api.modules.podcasts.api import PodcastMetadata, PodcastMetadataSet


def test_phase2b_tables_are_registered() -> None:
    assert {
        "active_model_configuration",
        "asset_migration_entries",
        "asset_migration_manifests",
        "podcast_episode_audio_variants",
        "podcast_episode_translations",
        "podcast_episodes",
    } <= set(Base.metadata.tables)


def test_podcast_trading_date_is_unique() -> None:
    table = Base.metadata.tables["podcast_episodes"]
    assert table.columns["trading_date"].unique


def test_only_one_active_audio_variant_per_episode_locale() -> None:
    table = Base.metadata.tables["podcast_episode_audio_variants"]
    index = next(
        value for value in table.indexes if value.name == "uq_podcast_episode_audio_variant_active"
    )
    assert index.unique
    assert str(index.dialect_options["postgresql"]["where"]) == "is_active"


def test_podcast_audio_schema_has_no_analysis_or_transcript_state() -> None:
    table = Base.metadata.tables["podcast_episode_audio_variants"]
    assert {
        "analysis_status",
        "analysis_error",
        "analyzed_at",
        "transcript",
    }.isdisjoint(table.columns)
    constraints = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if hasattr(constraint, "sqltext")
    }
    assert "chapters_source IN ('none', 'file', 'manual')" in constraints


def test_retained_chat_and_generation_foreign_keys_do_not_cascade() -> None:
    messages = Base.metadata.tables["messages"]
    generations = Base.metadata.tables["generation_records"]
    message_fk = next(iter(messages.columns["conversation_id"].foreign_keys))
    generation_fk = next(iter(generations.columns["message_id"].foreign_keys))
    assert message_fk.ondelete == "RESTRICT"
    assert generation_fk.ondelete == "RESTRICT"


def test_global_model_uses_singleton_pointer_as_only_active_source() -> None:
    configurations = Base.metadata.tables["model_configurations"]
    active = Base.metadata.tables["active_model_configuration"]
    assert "is_active" not in configurations.columns
    assert active.columns["singleton_id"].primary_key
    assert active.columns["model_configuration_id"].unique


def test_active_asset_requires_verified_metadata() -> None:
    table = Base.metadata.tables["assets"]
    constraints = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if hasattr(constraint, "sqltext")
    }
    assert (
        "status != 'active' OR (size_bytes > 0 AND sha256 IS NOT NULL AND char_length(sha256) = 64)"
    ) in constraints


def test_complete_podcast_metadata_requires_exactly_three_locales() -> None:
    complete = PodcastMetadataSet(
        values=(
            PodcastMetadata(locale="zh-hant", title="市場晨報", summary="摘要"),
            PodcastMetadata(locale="zh-hans", title="市场晨报", summary="摘要"),
            PodcastMetadata(locale="en", title="Market Brief", summary="Summary"),
        )
    )
    assert {value.locale for value in complete.values} == {"zh-hant", "zh-hans", "en"}

    with pytest.raises(ValidationError, match="exactly"):
        PodcastMetadataSet(
            values=(
                PodcastMetadata(locale="zh-hant", title="市場晨報", summary="摘要"),
                PodcastMetadata(locale="en", title="Market Brief", summary="Summary"),
            )
        )
