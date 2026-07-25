import hashlib
import uuid
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from daily_insights_api.core.enums import AssetKind, AssetStatus
from daily_insights_api.modules.assets.models import Asset
from daily_insights_api.modules.assets.object_store import ObjectRef, ObjectStore

Locale = Literal["zh-hant", "zh-hans", "en"]
MigrationStatus = Literal["planned", "verified", "cutover", "failed"]
EntryStatus = Literal["planned", "verified", "cutover", "failed"]
MigrationIdentity = tuple[uuid.UUID, ObjectRef, ObjectRef, date, Locale]

_AUDIO_EXTENSIONS = {
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}
ALLOWED_PODCAST_AUDIO_MIME_TYPES = frozenset(_AUDIO_EXTENSIONS)


class AssetContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssetForSigning(AssetContract):
    id: uuid.UUID
    ref: ObjectRef
    kind: AssetKind
    status: AssetStatus
    mime_type: str
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SignedAsset(AssetContract):
    asset_id: uuid.UUID
    url: str
    expires_in_seconds: int


class AssetMigrationInput(AssetContract):
    asset_id: uuid.UUID
    source: ObjectRef
    target_bucket: str = Field(min_length=1, max_length=100)
    trading_date: date
    locale: Locale = "zh-hant"
    expected_mime_type: str

    @property
    def target(self) -> ObjectRef:
        return ObjectRef(
            bucket=self.target_bucket,
            key=canonical_podcast_audio_key(
                trading_date=self.trading_date,
                locale=self.locale,
                asset_id=self.asset_id,
                mime_type=self.expected_mime_type,
            ),
        )


class MigratedObject(AssetContract):
    asset_id: uuid.UUID
    source: ObjectRef
    target: ObjectRef
    trading_date: date
    locale: Locale
    size_bytes: int | None = None
    mime_type: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: EntryStatus
    error_code: str | None = None

    @model_validator(mode="after")
    def require_verified_evidence(self) -> Self:
        if self.status in {"verified", "cutover"}:
            if self.size_bytes is None or self.size_bytes <= 0:
                raise ValueError("verified migration entry requires a positive size")
            if self.mime_type not in ALLOWED_PODCAST_AUDIO_MIME_TYPES:
                raise ValueError("verified migration entry requires an allowed MIME type")
            if self.sha256 is None:
                raise ValueError("verified migration entry requires SHA-256")
            if self.error_code is not None:
                raise ValueError("verified migration entry cannot contain an error")
            expected_key = canonical_podcast_audio_key(
                trading_date=self.trading_date,
                locale=self.locale,
                asset_id=self.asset_id,
                mime_type=self.mime_type,
            )
            if self.target.key != expected_key:
                raise ValueError("verified migration entry target is not canonical")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed migration entry requires an error code")
        return self


class AssetMigrationResult(AssetContract):
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    dry_run: bool
    status: MigrationStatus
    entries: tuple[MigratedObject, ...]

    @model_validator(mode="after")
    def require_reconciled_manifest(self) -> Self:
        if not self.entries:
            raise ValueError("migration manifest requires at least one entry")
        expected_id = migrated_objects_idempotency_key(self.entries)
        if self.idempotency_key != expected_id:
            raise ValueError("migration manifest idempotency key does not match entries")
        statuses = {entry.status for entry in self.entries}
        if self.status == "verified" and (self.dry_run or statuses != {"verified"}):
            raise ValueError("verified manifest requires verified non-dry-run entries")
        if self.status == "cutover" and (self.dry_run or statuses != {"cutover"}):
            raise ValueError("cutover manifest requires cutover non-dry-run entries")
        if self.status == "failed" and "failed" not in statuses:
            raise ValueError("failed manifest requires a failed entry")
        if self.status == "planned" and not self.dry_run:
            raise ValueError("planned manifest must be a dry run")
        return self


def canonical_podcast_audio_key(
    *,
    trading_date: date,
    locale: Locale,
    asset_id: uuid.UUID,
    mime_type: str,
) -> str:
    try:
        extension = _AUDIO_EXTENSIONS[mime_type.lower()]
    except KeyError as error:
        raise ValueError("unsupported Podcast audio MIME type") from error
    day = trading_date.isoformat()
    return f"podcasts/{day}/audio/{locale}/podcast-{day}-{locale}-{asset_id}.{extension}"


def migration_idempotency_key(entries: tuple[AssetMigrationInput, ...]) -> str:
    return _migration_idempotency_key(
        (
            entry.asset_id,
            entry.source,
            entry.target,
            entry.trading_date,
            entry.locale,
        )
        for entry in entries
    )


def migrated_objects_idempotency_key(entries: tuple[MigratedObject, ...]) -> str:
    return _migration_idempotency_key(
        (
            entry.asset_id,
            entry.source,
            entry.target,
            entry.trading_date,
            entry.locale,
        )
        for entry in entries
    )


def _migration_idempotency_key(
    values: Iterable[MigrationIdentity],
) -> str:
    canonical = "\n".join(
        f"{asset_id}|{source.bucket}|{source.key}|{target.bucket}|{target.key}|"
        f"{trading_date.isoformat()}|{locale}"
        for asset_id, source, target, trading_date, locale in sorted(
            values, key=lambda value: str(value[0])
        )
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def load_asset_for_signing(
    database: AsyncSession,
    asset_id: uuid.UUID,
) -> AssetForSigning | None:
    from daily_insights_api.modules.assets.service import (
        load_asset_for_signing as load,
    )

    return await load(database, asset_id)


async def sign_asset_download(
    store: ObjectStore,
    asset: AssetForSigning,
    *,
    expires_in: timedelta,
) -> SignedAsset:
    from daily_insights_api.modules.assets.service import sign_asset_download as sign

    return await sign(store, asset, expires_in=expires_in)


__all__ = [
    "ALLOWED_PODCAST_AUDIO_MIME_TYPES",
    "Asset",
    "AssetForSigning",
    "AssetMigrationInput",
    "AssetMigrationResult",
    "AssetStatus",
    "MigratedObject",
    "ObjectRef",
    "ObjectStore",
    "SignedAsset",
    "canonical_podcast_audio_key",
    "load_asset_for_signing",
    "migrate_podcast_assets",
    "sign_asset_download",
]


async def migrate_podcast_assets(
    store: ObjectStore,
    entries: tuple[AssetMigrationInput, ...],
    *,
    dry_run: bool,
) -> AssetMigrationResult:
    from daily_insights_api.modules.assets.service import migrate_podcast_assets as migrate

    return await migrate(store, entries, dry_run=dry_run)
