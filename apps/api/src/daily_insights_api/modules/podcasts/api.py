import uuid
from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Locale = Literal["zh-hant", "zh-hans", "en"]
SUPPORTED_LOCALES = frozenset(("zh-hant", "zh-hans", "en"))
DEFAULT_AUDIO_LOCALE: Locale = "zh-hant"


class PodcastContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PodcastMetadata(PodcastContract):
    locale: Locale
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=10_000)


class PodcastMetadataSet(PodcastContract):
    values: tuple[PodcastMetadata, ...]

    @model_validator(mode="after")
    def require_all_locales(self) -> Self:
        locales = [metadata.locale for metadata in self.values]
        if len(locales) != len(set(locales)):
            raise ValueError("Podcast metadata locales must be unique")
        if set(locales) != SUPPORTED_LOCALES:
            raise ValueError("Podcast metadata must contain exactly zh-hant, zh-hans, and en")
        return self


class PodcastAudioVariant(PodcastContract):
    asset_id: uuid.UUID
    locale: Locale
    version: int = Field(gt=0)


class ResolvedPodcastAudio(PodcastContract):
    requested_locale: Locale
    resolved_locale: Locale
    variant: PodcastAudioVariant


class AudioReplacement(PodcastContract):
    locale: Locale
    expected_current_version: int
    next_version: int
    new_asset_id: uuid.UUID


class AudioVariantConflictError(RuntimeError):
    pass


class AudioVariantUnavailableError(LookupError):
    pass


class PodcastEpisodeCreate(PodcastContract):
    trading_date: date
    metadata: PodcastMetadataSet
    reason: str = Field(min_length=1, max_length=2_000)


class PodcastEpisodeUpdate(PodcastContract):
    expected_version: int = Field(gt=0)
    metadata: PodcastMetadataSet
    reason: str = Field(min_length=1, max_length=2_000)


class PodcastPublicationRequest(PodcastContract):
    expected_version: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=2_000)


class PodcastAudioImportRequest(PodcastContract):
    source_bucket: str = Field(min_length=1, max_length=100)
    source_key: str = Field(min_length=1, max_length=1_024)
    locale: Locale = "zh-hant"
    expected_mime_type: str = Field(min_length=1, max_length=255)
    confirm_replacement: bool = False
    expected_current_version: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=2_000)


class PodcastAudioVariantResponse(PodcastContract):
    asset_id: uuid.UUID
    locale: Locale
    version: int
    is_active: bool


class PodcastEpisodeAdminResponse(PodcastContract):
    id: uuid.UUID
    trading_date: date
    status: Literal["draft", "published"]
    version: int
    metadata: tuple[PodcastMetadata, ...]
    audio_variants: tuple[PodcastAudioVariantResponse, ...]
    cover_asset_id: uuid.UUID | None
    published_at: datetime | None


class PodcastEpisodeSummaryResponse(PodcastContract):
    id: uuid.UUID
    trading_date: date
    title: str
    summary: str
    locale: Locale
    cover_asset_id: uuid.UUID | None


class PodcastEpisodeDetailResponse(PodcastEpisodeSummaryResponse):
    published_at: datetime


class PodcastAudioPlaybackResponse(PodcastContract):
    episode_id: uuid.UUID
    requested_locale: Locale
    resolved_locale: Locale
    asset_id: uuid.UUID
    url: str
    expires_in_seconds: int


class PodcastAudioReplacementRequired(PodcastContract):
    code: Literal["replacement_confirmation_required"] = "replacement_confirmation_required"
    current_version: int


def resolve_audio_variant(
    variants: tuple[PodcastAudioVariant, ...],
    requested_locale: Locale,
) -> ResolvedPodcastAudio:
    by_locale = {variant.locale: variant for variant in variants}
    variant = by_locale.get(requested_locale) or by_locale.get(DEFAULT_AUDIO_LOCALE)
    if variant is None:
        raise AudioVariantUnavailableError("no requested or zh-hant Podcast audio is available")
    return ResolvedPodcastAudio(
        requested_locale=requested_locale,
        resolved_locale=variant.locale,
        variant=variant,
    )


def prepare_audio_replacement(
    *,
    current: PodcastAudioVariant,
    expected_current_version: int,
    new_asset_id: uuid.UUID,
) -> AudioReplacement:
    if current.version != expected_current_version:
        raise AudioVariantConflictError("Podcast audio version changed; reload before replacing")
    return AudioReplacement(
        locale=current.locale,
        expected_current_version=current.version,
        next_version=current.version + 1,
        new_asset_id=new_asset_id,
    )
