import type { Locale } from "@daily-insights/api-client"
import { z } from "zod"

export const podcastProgressSchema = z.object({
  positionSeconds: z.number().nonnegative(),
  durationSeconds: z.number().nonnegative(),
  updatedAt: z.iso.datetime(),
})
export type PodcastProgress = z.infer<typeof podcastProgressSchema>

// Audio is resolved per locale with a fallback order (see the podcasts API),
// so a listener's progress lives under the locale that actually played.
const audioLocaleFallbackOrder: Locale[] = ["zh-hant", "zh-hans", "en"]

export function podcastProgressKey(
  userId: string,
  episodeId: string,
  resolvedLocale: Locale
) {
  return `daily-insights:podcast-progress:v1:${userId}:${episodeId}:${resolvedLocale}`
}

export function parsePodcastProgress(
  raw: string | null
): PodcastProgress | null {
  if (!raw) return null
  try {
    const parsed = podcastProgressSchema.safeParse(JSON.parse(raw))
    return parsed.success ? parsed.data : null
  } catch {
    return null
  }
}

export function restoredPosition(raw: string | null, duration: number) {
  if (!Number.isFinite(duration) || duration <= 0) return null
  const progress = parsePodcastProgress(raw)
  if (!progress) return null
  return Math.min(progress.positionSeconds, Math.max(0, duration - 1))
}

/** Progress for an episode: the requested locale first, then whichever
 * fallback edition the listener last heard. Storage failures read as
 * "never played". */
export function readPodcastProgress(
  userId: string,
  episodeId: string,
  locale: Locale
): PodcastProgress | null {
  const locales = [
    locale,
    ...audioLocaleFallbackOrder.filter(candidate => candidate !== locale),
  ]
  for (const candidate of locales) {
    try {
      const progress = parsePodcastProgress(
        localStorage.getItem(podcastProgressKey(userId, episodeId, candidate))
      )
      if (progress) return progress
    } catch {
      return null
    }
  }
  return null
}

export function writePodcastProgress(
  userId: string,
  episodeId: string,
  resolvedLocale: Locale,
  progress: { positionSeconds: number; durationSeconds: number }
) {
  try {
    localStorage.setItem(
      podcastProgressKey(userId, episodeId, resolvedLocale),
      JSON.stringify({
        positionSeconds: progress.positionSeconds,
        durationSeconds: progress.durationSeconds,
        updatedAt: new Date().toISOString(),
      } satisfies PodcastProgress)
    )
  } catch {
    // Storage must never block playback.
  }
}
