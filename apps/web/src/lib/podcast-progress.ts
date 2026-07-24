import type { Locale } from "@daily-insights/api-client"
import { z } from "zod"

export const podcastProgressSchema = z.object({
  positionSeconds: z.number().nonnegative(),
  durationSeconds: z.number().nonnegative(),
  updatedAt: z.iso.datetime(),
})

export function podcastProgressKey(
  userId: string,
  episodeId: string,
  resolvedLocale: Locale
) {
  return `daily-insights:podcast-progress:v1:${userId}:${episodeId}:${resolvedLocale}`
}

export function restoredPosition(raw: string | null, duration: number) {
  if (!raw || !Number.isFinite(duration) || duration <= 0) return null
  try {
    const parsed = podcastProgressSchema.safeParse(JSON.parse(raw))
    if (!parsed.success) return null
    return Math.min(parsed.data.positionSeconds, Math.max(0, duration - 1))
  } catch {
    return null
  }
}
