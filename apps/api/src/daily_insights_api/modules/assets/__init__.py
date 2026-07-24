"""Public contracts for shared private object assets."""

from daily_insights_api.modules.assets.api import (
    AssetForSigning,
    AssetMigrationInput,
    AssetMigrationResult,
    MigratedObject,
    canonical_podcast_audio_key,
)
from daily_insights_api.modules.assets.object_store import (
    ObjectMetadata,
    ObjectRef,
    ObjectStore,
)
from daily_insights_api.modules.assets.service import (
    AssetMigrationError,
    AssetNotSignableError,
    cutover_migration,
    migrate_podcast_assets,
    sign_asset_download,
)

__all__ = [
    "AssetForSigning",
    "AssetMigrationError",
    "AssetMigrationInput",
    "AssetMigrationResult",
    "AssetNotSignableError",
    "MigratedObject",
    "ObjectMetadata",
    "ObjectRef",
    "ObjectStore",
    "canonical_podcast_audio_key",
    "cutover_migration",
    "migrate_podcast_assets",
    "sign_asset_download",
]
