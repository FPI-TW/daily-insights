import { ApiError, type DataManagementRun } from "@daily-insights/api-client"
import type { Locale } from "@daily-insights/api-client"
import { numberLocales } from "#/lib/format"

export function formatNewsTime(value: string, locale: Locale) {
  return new Intl.DateTimeFormat(numberLocales[locale], {
    timeZone: "Asia/Taipei",
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(value))
}

/** Only safe GETs opt in; mutations must never inherit automatic retries. */
export function retryNewsAdminGet(failureCount: number, error: unknown) {
  return (
    failureCount < 2 &&
    (error instanceof TypeError ||
      (error instanceof ApiError && [502, 503, 504].includes(error.status)))
  )
}

export function newsAdminRetryDelay(attempt: number) {
  return attempt === 0 ? 2_000 : 5_000
}

export function newsRunState(run: DataManagementRun, now = Date.now()) {
  if (run.error === "news_window_expired") return "expired"
  if (run.status === "pending") {
    return run.scheduled_for && new Date(run.scheduled_for).getTime() > now
      ? "scheduled"
      : "waiting_worker"
  }
  if (run.status === "running") {
    const heartbeat = run.heartbeat_at ?? run.started_at
    return heartbeat && now - new Date(heartbeat).getTime() > 90_000
      ? "unresponsive"
      : "running"
  }
  if (run.status === "cancelled") return "cancelled"
  const states = Object.values(run.news ?? {}).map(progress => progress.state)
  if (states.includes("needs_attention")) return "needs_attention"
  if (states.includes("expired")) return "expired"
  if (states.includes("waiting_retry")) return "waiting_retry"
  return run.status === "succeeded" ? "completed" : "needs_attention"
}

export function canResumeNews(run: DataManagementRun, today: string) {
  return (
    run.edition_date === today &&
    (run.status === "failed" ||
      run.status === "partial" ||
      run.error === "news_window_expired")
  )
}
