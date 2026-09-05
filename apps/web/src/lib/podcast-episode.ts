import type { Locale, PodcastChapter } from "@daily-insights/api-client"
import { numberLocales } from "#/lib/format"

export const playbackSpeeds = [1, 1.25, 1.5, 2] as const
export type PlaybackSpeed = (typeof playbackSpeeds)[number]
export const skipSeconds = 15
export const waveformBarCount = 64
export const collapsedListSize = 4

export type EpisodeFilter = "all" | "unheard" | "heard"
export const episodeFilters: EpisodeFilter[] = ["all", "unheard", "heard"]

export function nextPlaybackSpeed(speed: PlaybackSpeed): PlaybackSpeed {
  const index = playbackSpeeds.indexOf(speed)
  return playbackSpeeds[(index + 1) % playbackSpeeds.length] ?? 1
}

export function formatSpeed(speed: PlaybackSpeed) {
  return `${Number.isInteger(speed) ? speed.toFixed(1) : String(speed)}×`
}

/** `m:ss` clock time; hours fold into minutes (a briefing never reaches an
 * hour, and a fixed width keeps the tabular figures aligned). */
export function clockTime(seconds: number) {
  const whole = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0))
  const minutes = Math.floor(whole / 60)
  const rest = whole % 60
  return `${minutes}:${String(rest).padStart(2, "0")}`
}

/** `YYYY.MM.DD` from an ISO date; locale-independent by design. */
export function dotDate(isoDate: string) {
  return isoDate.replaceAll("-", ".")
}

function utcDate(isoDate: string) {
  return new Date(`${isoDate}T00:00:00Z`)
}

export function weekdayLabel(isoDate: string, locale: Locale) {
  return new Intl.DateTimeFormat(numberLocales[locale], {
    weekday: "short",
    timeZone: "UTC",
  }).format(utcDate(isoDate))
}

/** Release time (`07:30`) of an episode in the viewer's zone, from when its
 * audio file was registered. */
export function releaseTimeLabel(
  isoDateTime: string,
  locale: Locale,
  timeZone?: string
) {
  const instant = new Date(isoDateTime)
  if (Number.isNaN(instant.getTime())) return null
  return new Intl.DateTimeFormat(numberLocales[locale], {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    ...(timeZone ? { timeZone } : {}),
  }).format(instant)
}

/** Month and day without the year, e.g. `9月2日` or `Sep 2`. */
export function monthDayLabel(isoDate: string, locale: Locale) {
  return new Intl.DateTimeFormat(numberLocales[locale], {
    month: locale === "en" ? "short" : "long",
    day: "numeric",
    timeZone: "UTC",
  }).format(utcDate(isoDate))
}

/** Deterministic pseudo-waveform: stable per episode so the bars never
 * change between renders. Heights are 8–50px for the waveform style and a
 * flat 4px line otherwise. */
export function waveformHeights(
  durationSeconds: number,
  style: "waveform" | "line" = "waveform",
  count = waveformBarCount
) {
  return Array.from({ length: count }, (_, index) => {
    if (style === "line") return 4
    const seed = index * 0.9 + durationSeconds * 0.01
    return Math.round(
      8 + Math.abs(Math.sin(seed) * 28 + Math.cos(index * 0.37) * 14)
    )
  })
}

export type ChapterSegment = PodcastChapter & {
  index: number
  lengthSeconds: number
}

/** Chapters sorted by start; each segment runs to the next start or the
 * episode end. */
export function chapterSegments(
  chapters: readonly PodcastChapter[],
  durationSeconds: number
): ChapterSegment[] {
  const sorted = [...chapters].sort((a, b) => a.start_seconds - b.start_seconds)
  return sorted.map((chapter, index) => {
    const end = sorted[index + 1]?.start_seconds ?? durationSeconds
    return {
      ...chapter,
      index,
      lengthSeconds: Math.max(0, end - chapter.start_seconds),
    }
  })
}

export function currentChapterIndex(
  segments: readonly ChapterSegment[],
  positionSeconds: number
) {
  let current = 0
  segments.forEach((segment, index) => {
    if (positionSeconds >= segment.start_seconds) current = index
  })
  return current
}

export type ListeningStatus =
  | { kind: "unheard" }
  | { kind: "partial"; percent: number }
  | { kind: "finished" }

// An episode counts as finished once the listener is within the last second
// (`ended` fires at the exact duration, but a restored position is clamped
// one second short of it).
export function listeningStatus(
  positionSeconds: number,
  durationSeconds: number | null
): ListeningStatus {
  if (positionSeconds <= 0) return { kind: "unheard" }
  if (durationSeconds && positionSeconds >= durationSeconds - 1) {
    return { kind: "finished" }
  }
  if (!durationSeconds) return { kind: "partial", percent: 0 }
  return {
    kind: "partial",
    percent: Math.min(
      99,
      Math.max(1, Math.round((positionSeconds / durationSeconds) * 100))
    ),
  }
}

export function matchesFilter(filter: EpisodeFilter, status: ListeningStatus) {
  if (filter === "all") return true
  if (filter === "unheard") return status.kind === "unheard"
  return status.kind !== "unheard"
}

export function clampPosition(seconds: number, durationSeconds: number) {
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    return Math.max(0, seconds)
  }
  return Math.min(durationSeconds, Math.max(0, seconds))
}
