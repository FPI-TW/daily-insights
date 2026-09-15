import {
  ApiError,
  type DataManagementRun,
  type NewsAdminCandidate,
  type NewsCandidateStage,
} from "@daily-insights/api-client"

// The admin table groups the six pipeline stages into the three questions an
// editor asks: what did the model reject, what did it never pick, and what
// never reached it. "all" keeps the raw list.
export type CandidateFilter = "all" | "dropped" | "reviewed" | "other"
export const candidateFilters: readonly CandidateFilter[] = [
  "all",
  "dropped",
  "reviewed",
  "other",
]

export function candidateFilterOf(
  stage: NewsCandidateStage
): Exclude<CandidateFilter, "all"> {
  if (stage === "dropped") return "dropped"
  if (stage === "reviewed") return "reviewed"
  return "other"
}

export function filterCandidates(
  candidates: readonly NewsAdminCandidate[],
  filter: CandidateFilter
) {
  if (filter === "all") return [...candidates]
  return candidates.filter(
    candidate => candidateFilterOf(candidate.stage) === filter
  )
}

export function isActiveRun(run: Pick<DataManagementRun, "status">) {
  return run.status === "pending" || run.status === "running"
}

// news_all, news_market and news_publish all share the prefix; the curation
// view polls while any of them can still change an edition.
export function isNewsRun(run: Pick<DataManagementRun, "operation">) {
  return run.operation.startsWith("news")
}

export function activePublishRunIds(runs: readonly DataManagementRun[]) {
  return new Set(
    runs
      .filter(run => run.operation === "news_publish" && isActiveRun(run))
      .map(run => run.id)
  )
}

/** Pick the i18n key for a failed admin news mutation. */
export function newsMutationErrorKey(
  error: unknown,
  failedKey: string
): "newsManagementConflict" | "newsManagementUnavailable" | string {
  if (error instanceof ApiError && error.status === 409) {
    return "newsManagementConflict"
  }
  if (error instanceof ApiError && error.status === 503) {
    return "newsManagementUnavailable"
  }
  return failedKey
}

/** Human summary of a news_publish run result: published and failed counts. */
export function publishRunCounts(result: Record<string, unknown> | null) {
  if (!result) return null
  const published = Number(result.published)
  const failed = Number(result.failed)
  if (!Number.isFinite(published) || !Number.isFinite(failed)) return null
  return { published, failed }
}
