import uuid
from datetime import date, datetime
from itertools import pairwise
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Locale = Literal["zh-hant", "zh-hans", "en"]
PodcastUploadReason = Literal["initial_upload", "update_file", "other"]
MetadataSource = Literal["derived", "manual"]
ChaptersSource = Literal["none", "file", "manual"]
SUPPORTED_LOCALES = frozenset(("zh-hant", "zh-hans", "en"))
DEFAULT_AUDIO_LOCALE: Locale = "zh-hant"
PODCAST_AUDIO_FALLBACK_ORDER: tuple[Locale, ...] = ("zh-hant", "zh-hans", "en")


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


MAX_PODCAST_CHAPTERS = 20


class PodcastChapter(PodcastContract):
    """A navigation marker: the segment runs from `start_seconds` to the next
    chapter's start (or the end of the audio)."""

    start_seconds: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=120)


def validate_chapters(
    chapters: tuple[PodcastChapter, ...],
    *,
    duration_seconds: int | None = None,
) -> tuple[PodcastChapter, ...]:
    """Chapters must be few, strictly ascending, and inside the audio."""
    if len(chapters) > MAX_PODCAST_CHAPTERS:
        raise ValueError(f"at most {MAX_PODCAST_CHAPTERS} chapters are allowed")
    for previous, current in pairwise(chapters):
        if current.start_seconds <= previous.start_seconds:
            raise ValueError("chapter start times must be strictly ascending")
    if duration_seconds is not None and chapters:
        if chapters[-1].start_seconds >= duration_seconds:
            raise ValueError("chapter start times must fall inside the audio")
    return chapters


class PodcastChaptersUpdate(PodcastContract):
    expected_version: int = Field(gt=0)
    chapters: tuple[PodcastChapter, ...]
    reason: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def chapters_well_formed(self) -> Self:
        validate_chapters(self.chapters)
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
    duration_seconds: int | None = None
    chapters: tuple[PodcastChapter, ...] = ()
    chapters_source: ChaptersSource = "none"


class PodcastEpisodeAdminResponse(PodcastContract):
    id: uuid.UUID
    trading_date: date
    status: Literal["draft", "published"]
    version: int
    metadata: tuple[PodcastMetadata, ...]
    metadata_source: MetadataSource = "derived"
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
    # Length of the audio the player will resolve for this locale; null until
    # an uploaded file could be measured.
    duration_seconds: int | None = None
    # When the audio the player will resolve was registered; shown as the
    # episode's release time. Null until an audio file exists.
    audio_created_at: datetime | None = None
    # Chapter markers of the audio the player will resolve; empty when the
    # file carried none and nobody added any.
    chapters: tuple[PodcastChapter, ...] = ()


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
    variant = by_locale.get(requested_locale)
    if variant is None:
        variant = next(
            (by_locale[locale] for locale in PODCAST_AUDIO_FALLBACK_ORDER if locale in by_locale),
            None,
        )
    if variant is None:
        raise AudioVariantUnavailableError("no Podcast audio is available")
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
