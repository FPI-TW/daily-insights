"""Podcast analysis: transcribe an uploaded recording and derive its title,
summary and chapters with the language model.

The pipeline is deliberately two-step. Speech-to-text (OpenAI transcription)
is the only part that needs audio; everything editorial happens on the timed
transcript with the same DeepSeek endpoint the daily news uses. Chapter
starts are chosen from transcript block boundaries rather than invented, so
they always land on a real sentence start.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daily_insights_api.core.config import Settings
from daily_insights_api.core.observability import emit_event
from daily_insights_api.modules.assets.api import Asset, ObjectRef, ObjectStore
from daily_insights_api.modules.audit.api import record_audit_event
from daily_insights_api.modules.podcasts.api import (
    Locale,
    PodcastChapter,
    PodcastMetadata,
    PodcastMetadataSet,
    validate_chapters,
)
from daily_insights_api.modules.podcasts.models import (
    PodcastEpisode,
    PodcastEpisodeAudioVariant,
)
from daily_insights_api.modules.podcasts.prompts import load_analysis_rules

# OpenAI rejects transcription uploads above 25 MB.
MAX_TRANSCRIPTION_BYTES = 25 * 1024 * 1024
# Transcript segments are merged into blocks of at least this length so an
# eight-minute briefing becomes ~25 candidates for chapter starts instead of
# ~150 sentences.
MIN_BLOCK_SECONDS = 20
MAX_PROMPT_CHARACTERS = 24_000
MIN_AI_CHAPTERS = 3
MAX_AI_CHAPTERS = 6
# Titles are capped per script: 40 CJK characters carry as much as ~90
# Latin characters, and an English title cut at 40 lands mid-word.
TITLE_MAX: dict[str, int] = {"zh-hant": 40, "zh-hans": 40, "en": 90}
SUMMARY_MAX = 600
TRANSCRIPTION_LANGUAGES: dict[str, str] = {"zh-hant": "zh", "zh-hans": "zh", "en": "en"}
# Whisper writes Chinese in whichever script it guesses (usually Simplified)
# and mishears domain terms; a short prompt in the expected script with the
# vocabulary of a market briefing steers both.
TRANSCRIPTION_PROMPTS: dict[str, str] = {
    "zh-hant": (
        "以下是台灣投資人的繁體中文晨間市場 Podcast，"  # noqa: RUF001
        "談聯準會、台股、外資、新台幣匯率與台積電。"
    ),
    "zh-hans": (
        "以下是简体中文的晨间市场 Podcast，"  # noqa: RUF001
        "谈联准会、台股、外资、新台币汇率与台积电。"
    ),
    "en": (
        "The following is an English morning market briefing on the Fed, Taiwan stocks, "
        "foreign investors, the NT dollar and TSMC."
    ),
}
LOCALES: tuple[Locale, ...] = ("zh-hant", "zh-hans", "en")
ANALYSIS_PROMPT_VERSION = "podcast-analysis-v1"


class PodcastAnalysisError(RuntimeError):
    pass


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    language: str | None
    duration: float | None
    text: str
    segments: tuple[TranscriptSegment, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "duration": self.duration,
            "text": self.text,
            "segments": [
                {"start": item.start, "end": item.end, "text": item.text} for item in self.segments
            ],
        }


class Transcriber(Protocol):
    async def transcribe(
        self,
        content: bytes,
        *,
        filename: str,
        mime_type: str,
        language: str | None,
        prompt: str | None = None,
    ) -> Transcript: ...


class AnalysisModel(Protocol):
    async def complete_json(self, prompt: dict[str, Any]) -> dict[str, Any]: ...


class OpenAITranscriber:
    """`POST /audio/transcriptions` with `verbose_json` so segments carry
    start/end seconds (supported by whisper-1)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    async def transcribe(
        self,
        content: bytes,
        *,
        filename: str,
        mime_type: str,
        language: str | None,
        prompt: str | None = None,
    ) -> Transcript:
        data: dict[str, str] = {
            "model": self._model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
        }
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout), trust_env=False
            ) as client:
                response = await client.post(
                    f"{self._base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    data=data,
                    files={"file": (filename, content, mime_type)},
                )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as error:
            raise PodcastAnalysisError(
                f"transcription request failed with HTTP {error.response.status_code}"
            ) from error
        except Exception as error:
            raise PodcastAnalysisError("transcription request failed") from error
        return parse_transcription(payload)


def parse_transcription(payload: object) -> Transcript:
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise PodcastAnalysisError("transcription response is not a verbose_json object")
    segments: list[TranscriptSegment] = []
    raw_segments = payload.get("segments")
    if isinstance(raw_segments, list):
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item["start"])
                end = float(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            text = str(item.get("text") or "").strip()
            if text:
                segments.append(TranscriptSegment(start=start, end=end, text=text))
    duration_value = payload.get("duration")
    duration = float(duration_value) if isinstance(duration_value, int | float) else None
    language_value = payload.get("language")
    return Transcript(
        language=str(language_value) if language_value else None,
        duration=duration,
        text=payload["text"].strip(),
        segments=tuple(segments),
    )


class JsonChatModel:
    """OpenAI-compatible chat completion that must answer with one JSON
    object (the DeepSeek endpoint used for daily news)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    async def complete_json(self, prompt: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self._model,
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "You output only valid JSON."},
                {
                    "role": "user",
                    "content": json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
                },
            ],
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout), trust_env=False
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            response.raise_for_status()
            data = response.json()
            parsed = json.loads(data["choices"][0]["message"]["content"])
        except httpx.HTTPStatusError as error:
            raise PodcastAnalysisError(
                f"language model request failed with HTTP {error.response.status_code}"
            ) from error
        except Exception as error:
            raise PodcastAnalysisError("language model request failed") from error
        if not isinstance(parsed, dict):
            raise PodcastAnalysisError("language model result must be a JSON object")
        return parsed


@dataclass(frozen=True)
class TranscriptBlock:
    index: int
    start_seconds: int
    text: str


def transcript_blocks(transcript: Transcript) -> tuple[TranscriptBlock, ...]:
    """Merge consecutive segments into blocks of at least MIN_BLOCK_SECONDS.
    A transcript without segments becomes a single block at 0:00."""
    blocks: list[TranscriptBlock] = []
    current_start: float | None = None
    current_text: list[str] = []
    for segment in transcript.segments:
        if current_start is None:
            current_start = segment.start
        current_text.append(segment.text)
        if segment.end - current_start >= MIN_BLOCK_SECONDS:
            blocks.append(
                TranscriptBlock(
                    index=len(blocks),
                    start_seconds=max(0, int(current_start)),
                    text=" ".join(current_text),
                )
            )
            current_start = None
            current_text = []
    if current_text:
        blocks.append(
            TranscriptBlock(
                index=len(blocks),
                start_seconds=max(0, int(current_start or 0)),
                text=" ".join(current_text),
            )
        )
    if not blocks and transcript.text:
        blocks.append(TranscriptBlock(index=0, start_seconds=0, text=transcript.text))
    return tuple(blocks)


def analysis_prompt(
    blocks: tuple[TranscriptBlock, ...],
    *,
    trading_date: str,
    duration_seconds: int | None,
) -> dict[str, Any]:
    budget = MAX_PROMPT_CHARACTERS
    listed: list[dict[str, Any]] = []
    for block in blocks:
        text = block.text[: max(0, budget)]
        if not text:
            break
        budget -= len(text)
        listed.append({"block": block.index, "start_seconds": block.start_seconds, "text": text})
    rules = load_analysis_rules()
    return {
        "task": rules[0],
        "rules": list(rules[1:]),
        "output_contract": {
            "title": {"zh-hant": "string", "zh-hans": "string", "en": "string"},
            "summary": {"zh-hant": "string", "zh-hans": "string", "en": "string"},
            "chapters": [
                {
                    "block": "integer",
                    "title": {"zh-hant": "string", "zh-hans": "string", "en": "string"},
                }
            ],
        },
        "episode": {"trading_date": trading_date, "duration_seconds": duration_seconds},
        "blocks": listed,
        "prompt_version": ANALYSIS_PROMPT_VERSION,
    }


@dataclass(frozen=True)
class AnalysisResult:
    metadata: PodcastMetadataSet
    chapters: dict[Locale, tuple[PodcastChapter, ...]]


def _clip(text: str, limit: int) -> str:
    """Trim to `limit` characters, backing up to the last space when the cut
    would split a Latin word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if text[limit] != " " and " " in cut and cut.rfind(" ") > limit // 2:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-")


def _localized(value: object, *, field: str, limit: int | dict[str, int]) -> dict[Locale, str]:
    if not isinstance(value, dict):
        raise PodcastAnalysisError(f"{field} must be an object keyed by locale")
    result: dict[Locale, str] = {}
    for locale in LOCALES:
        text = value.get(locale)
        if not isinstance(text, str) or not text.strip():
            raise PodcastAnalysisError(f"{field} is missing the {locale} text")
        cap = limit[locale] if isinstance(limit, dict) else limit
        result[locale] = _clip(" ".join(text.split()), cap)
    return result


def parse_analysis(
    result: dict[str, Any],
    blocks: tuple[TranscriptBlock, ...],
    *,
    duration_seconds: int | None,
) -> AnalysisResult:
    titles = _localized(result.get("title"), field="title", limit=TITLE_MAX)
    summaries = _localized(result.get("summary"), field="summary", limit=SUMMARY_MAX)
    raw_chapters = result.get("chapters")
    if not isinstance(raw_chapters, list):
        raise PodcastAnalysisError("chapters must be a list")
    starts_by_index = {block.index: block.start_seconds for block in blocks}
    chapters: dict[Locale, list[PodcastChapter]] = {locale: [] for locale in LOCALES}
    last_start = -1
    for item in raw_chapters:
        if not isinstance(item, dict) or not isinstance(item.get("block"), int):
            raise PodcastAnalysisError("each chapter needs an integer block")
        start = starts_by_index.get(item["block"])
        if start is None:
            raise PodcastAnalysisError("chapter block is not in the transcript")
        if not chapters["zh-hant"]:
            # The first chapter always opens the episode.
            start = 0
        if start <= last_start:
            continue
        names = _localized(item.get("title"), field="chapter title", limit=120)
        for locale in LOCALES:
            chapters[locale].append(PodcastChapter(start_seconds=start, title=names[locale]))
        last_start = start
    if not MIN_AI_CHAPTERS <= len(chapters["zh-hant"]) <= MAX_AI_CHAPTERS:
        raise PodcastAnalysisError(
            f"expected {MIN_AI_CHAPTERS}-{MAX_AI_CHAPTERS} chapters, got {len(chapters['zh-hant'])}"
        )
    validated: dict[Locale, tuple[PodcastChapter, ...]] = {}
    for locale in LOCALES:
        try:
            validated[locale] = validate_chapters(
                tuple(chapters[locale]), duration_seconds=duration_seconds
            )
        except ValueError as error:
            raise PodcastAnalysisError(str(error)) from error
    return AnalysisResult(
        metadata=PodcastMetadataSet(
            values=tuple(
                PodcastMetadata(locale=locale, title=titles[locale], summary=summaries[locale])
                for locale in LOCALES
            )
        ),
        chapters=validated,
    )


async def _read_all(chunks: AsyncIterator[bytes]) -> bytes:
    parts: list[bytes] = []
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > MAX_TRANSCRIPTION_BYTES:
            raise PodcastAnalysisError("audio exceeds the 25 MB transcription limit")
        parts.append(chunk)
    return b"".join(parts)


class PodcastAnalyzer:
    """Runs one analysis per audio variant and records the outcome on it.

    `schedule` runs the analysis as a background task in the API process
    (the upload response does not wait); `analyze` runs it inline and is what
    the backfill script uses. Results never touch the episode version, and a
    manual title or chapter list is never overwritten."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ObjectStore,
        transcriber: Transcriber,
        model: AnalysisModel,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._transcriber = transcriber
        self._model = model
        self._tasks: set[asyncio.Task[str]] = set()

    def schedule(
        self,
        variant_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID | None,
        request_id: str | None = None,
    ) -> asyncio.Task[str]:
        task = asyncio.create_task(
            self.analyze(variant_id, actor_user_id=actor_user_id, request_id=request_id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_for_scheduled(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def analyze(
        self,
        variant_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID | None,
        request_id: str | None = None,
    ) -> str:
        started = await self._mark_pending(variant_id)
        if started is None:
            return "missing"
        locale, ref, mime_type, trading_date, duration_seconds = started
        try:
            content = await _read_all(self._store.read(ref))
            filename = f"podcast.{'mp4' if mime_type == 'audio/mp4' else 'mp3'}"
            transcript = await self._transcriber.transcribe(
                content,
                filename=filename,
                mime_type=mime_type,
                language=TRANSCRIPTION_LANGUAGES.get(locale),
                prompt=TRANSCRIPTION_PROMPTS.get(locale),
            )
            blocks = transcript_blocks(transcript)
            if not blocks:
                raise PodcastAnalysisError("transcription produced no text")
            prompt = analysis_prompt(
                blocks, trading_date=trading_date, duration_seconds=duration_seconds
            )
            result = parse_analysis(
                await self._model.complete_json(prompt),
                blocks,
                duration_seconds=duration_seconds,
            )
        except PodcastAnalysisError as error:
            await self._mark_failed(variant_id, str(error))
            emit_event("podcast.analysis.failed", variant_id=str(variant_id), reason=str(error))
            return "failed"
        except Exception as error:
            await self._mark_failed(variant_id, "unexpected analysis failure")
            emit_event(
                "podcast.analysis.failed",
                variant_id=str(variant_id),
                reason=type(error).__name__,
            )
            return "failed"
        await self._store_result(
            variant_id,
            transcript,
            result,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )
        emit_event(
            "podcast.analysis.succeeded",
            variant_id=str(variant_id),
            locale=locale,
            chapters=len(result.chapters["zh-hant"]),
            blocks=len(blocks),
        )
        return "succeeded"

    async def _mark_pending(
        self, variant_id: uuid.UUID
    ) -> tuple[str, ObjectRef, str, str, int | None] | None:
        async with self._session_factory() as database:
            row = (
                await database.execute(
                    select(PodcastEpisodeAudioVariant, Asset, PodcastEpisode)
                    .join(Asset, Asset.id == PodcastEpisodeAudioVariant.asset_id)
                    .join(
                        PodcastEpisode, PodcastEpisode.id == PodcastEpisodeAudioVariant.episode_id
                    )
                    .where(PodcastEpisodeAudioVariant.id == variant_id)
                    .with_for_update(of=PodcastEpisodeAudioVariant)
                )
            ).first()
            if row is None:
                return None
            variant, asset, episode = row
            variant.analysis_status = "pending"
            variant.analysis_error = None
            await database.commit()
            return (
                variant.locale,
                ObjectRef(bucket=asset.bucket, key=asset.object_key),
                asset.mime_type,
                episode.trading_date.isoformat(),
                variant.duration_seconds,
            )

    async def _mark_failed(self, variant_id: uuid.UUID, reason: str) -> None:
        async with self._session_factory() as database:
            variant = await database.get(PodcastEpisodeAudioVariant, variant_id)
            if variant is None:
                return
            variant.analysis_status = "failed"
            variant.analysis_error = reason[:500]
            variant.analyzed_at = datetime.now(UTC)
            await database.commit()

    async def _store_result(
        self,
        variant_id: uuid.UUID,
        transcript: Transcript,
        result: AnalysisResult,
        *,
        actor_user_id: uuid.UUID | None,
        request_id: str | None,
    ) -> None:
        # Imported here to keep service.py free of a circular import.
        from daily_insights_api.modules.podcasts.service import replace_metadata

        async with self._session_factory() as database:
            variant = await database.get(
                PodcastEpisodeAudioVariant, variant_id, with_for_update=True
            )
            if variant is None:
                return
            episode = await database.get(PodcastEpisode, variant.episode_id, with_for_update=True)
            if episode is None:
                return
            variant.transcript = transcript.to_json()
            variant.analysis_status = "succeeded"
            variant.analysis_error = None
            variant.analyzed_at = datetime.now(UTC)
            chapters_written = False
            if variant.chapters_source != "manual":
                variant.chapters = [
                    chapter.model_dump()
                    for chapter in result.chapters.get(cast(Locale, variant.locale), ())
                ]
                variant.chapters_source = "ai"
                chapters_written = True
            metadata_written = False
            if episode.metadata_source != "manual":
                await replace_metadata(database, episode, result.metadata)
                episode.metadata_source = "ai"
                metadata_written = True
            # The episode version is left alone: analysis is not an editorial
            # action, and bumping it would make the admin's next save (with the
            # version they loaded before the upload) fail for no reason.
            record_audit_event(
                database,
                actor_user_id=actor_user_id,
                action="podcast.audio_analyzed",
                target_type="podcast_episode_audio_variant",
                target_id=str(variant.id),
                after={
                    "locale": variant.locale,
                    "version": episode.version,
                    "chapters_written": chapters_written,
                    "metadata_written": metadata_written,
                    "prompt_version": ANALYSIS_PROMPT_VERSION,
                },
                request_id=request_id,
            )
            await database.commit()


def build_podcast_analyzer(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: ObjectStore,
) -> PodcastAnalyzer:
    if settings.openai_api_key is None or settings.model_api_key is None:
        raise ValueError("podcast analysis needs openai_api_key and model_api_key")
    return PodcastAnalyzer(
        session_factory=session_factory,
        store=store,
        transcriber=OpenAITranscriber(
            base_url=settings.openai_api_base_url,
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.transcription_model,
            timeout_seconds=settings.podcast_analysis_timeout_seconds,
        ),
        model=JsonChatModel(
            base_url=settings.model_api_base_url,
            api_key=settings.model_api_key.get_secret_value(),
            model=settings.model_name,
            timeout_seconds=settings.model_timeout_seconds,
        ),
    )
