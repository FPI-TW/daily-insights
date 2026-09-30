import type { NewsFeedPollSource } from "@daily-insights/api-client"

export type FeedPollHealth = "never" | "cooling" | "failing" | "ok"

/** Summarise the latest poll of one feed; each poll overwrites the last. */
export function feedPollHealth(
  source: Pick<
    NewsFeedPollSource,
    "last_attempt_at" | "last_success_at" | "cooldown_until" | "last_error_code"
  >,
  now: number
): FeedPollHealth {
  if (source.last_attempt_at === null) return "never"
  if (source.cooldown_until && Date.parse(source.cooldown_until) > now) {
    return "cooling"
  }
  // A success timestamp older than the latest attempt means that attempt failed.
  const lastAttemptFailed =
    source.last_success_at === null ||
    Date.parse(source.last_success_at) < Date.parse(source.last_attempt_at)
  return source.last_error_code || lastAttemptFailed ? "failing" : "ok"
}
