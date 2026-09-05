import type { PodcastChapter } from "@daily-insights/api-client"
import { clockTime } from "#/lib/podcast-episode"

export const maxPodcastChapters = 20

// The back office edits chapters as plain lines, one marker per line:
//   0:00 FOMC 決議
//   2:10 外資回補三大權值股
// A start time is `m:ss` or `h:mm:ss`; the rest of the line is the title.
const linePattern = /^\s*(\d{1,3}(?::[0-5]?\d){1,2})\s+(.+?)\s*$/

export type ChapterTextResult =
  | { ok: true; chapters: PodcastChapter[] }
  | { ok: false; line: number; code: ChapterTextError }

export type ChapterTextError =
  "format" | "order" | "too_many" | "beyond_duration"

function parseClock(text: string) {
  const parts = text.split(":").map(Number)
  return parts.reduce((total, part) => total * 60 + part, 0)
}

export function formatChapterText(chapters: readonly PodcastChapter[]) {
  return chapters
    .map(chapter => `${clockTime(chapter.start_seconds)} ${chapter.title}`)
    .join("\n")
}

export function parseChapterText(
  text: string,
  durationSeconds: number | null = null
): ChapterTextResult {
  const chapters: PodcastChapter[] = []
  const lines = text.split(/\r?\n/)
  for (const [index, raw] of lines.entries()) {
    if (raw.trim() === "") continue
    const match = linePattern.exec(raw)
    if (!match) return { ok: false, line: index + 1, code: "format" }
    const start = parseClock(match[1] ?? "")
    const title = (match[2] ?? "").slice(0, 120)
    const previous = chapters[chapters.length - 1]
    if (previous && start <= previous.start_seconds) {
      return { ok: false, line: index + 1, code: "order" }
    }
    if (durationSeconds !== null && start >= durationSeconds) {
      return { ok: false, line: index + 1, code: "beyond_duration" }
    }
    chapters.push({ start_seconds: start, title })
    if (chapters.length > maxPodcastChapters) {
      return { ok: false, line: index + 1, code: "too_many" }
    }
  }
  return { ok: true, chapters }
}
