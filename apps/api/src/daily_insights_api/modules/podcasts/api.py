import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Locale = Literal["zh-TW", "zh-CN", "en"]
SUPPORTED_LOCALES = frozenset(("zh-TW", "zh-CN", "en"))
DEFAULT_AUDIO_LOCALE: Locale = "zh-TW"


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
            raise ValueError("Podcast metadata must contain exactly zh-TW, zh-CN, and en")
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


def resolve_audio_variant(
    variants: tuple[PodcastAudioVariant, ...],
    requested_locale: Locale,
) -> ResolvedPodcastAudio:
    by_locale = {variant.locale: variant for variant in variants}
    variant = by_locale.get(requested_locale) or by_locale.get(DEFAULT_AUDIO_LOCALE)
    if variant is None:
        raise AudioVariantUnavailableError("no requested or zh-TW Podcast audio is available")
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
