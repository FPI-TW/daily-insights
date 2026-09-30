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

export type FeedPollSummary = {
  total: number
  ok: number
  // Cooling down or failing on the latest poll.
  issues: number
  never: number
  gaps: number
}

/** Health totals shown on the collapsed collection summary. */
export function summarizeFeedPolls(
  sources: readonly NewsFeedPollSource[],
  now: number
): FeedPollSummary {
  const summary = { total: sources.length, ok: 0, issues: 0, never: 0, gaps: 0 }
  for (const source of sources) {
    const health = feedPollHealth(source, now)
    if (health === "ok") summary.ok += 1
    else if (health === "never") summary.never += 1
    else summary.issues += 1
    summary.gaps += source.gap_count
  }
  return summary
}

// Failures first, then cooldowns, feeds with gaps tonight, feeds never polled,
// and healthy feeds last.
function attentionRank(source: NewsFeedPollSource, now: number) {
  const health = feedPollHealth(source, now)
  if (health === "failing") return 0
  if (health === "cooling") return 1
  if (source.gap_count > 0) return 2
  if (health === "never") return 3
  return 4
}

/** Order feeds so the ones needing attention lead, then by name. */
export function sortFeedPollSources(
  sources: readonly NewsFeedPollSource[],
  now: number
) {
  return [...sources].sort(
    (left, right) =>
      attentionRank(left, now) - attentionRank(right, now) ||
      left.source_name.localeCompare(right.source_name) ||
      left.source_key.localeCompare(right.source_key)
  )
}
