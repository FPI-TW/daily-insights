import { describe, expect, it } from "vitest"
import { ApiError, dataManagementRunSchema } from "@daily-insights/api-client"
import {
  canResumeNews,
  newsAdminRetryDelay,
  newsRunState,
  retryNewsAdminGet,
} from "./news-recovery"

const run = dataManagementRunSchema.parse({
  id: "00000000-0000-4000-8000-000000000001",
  operation: "news_market",
  market_code: "us_equity",
  edition_date: "2026-09-11",
  status: "pending",
  requested_by_user_id: null,
  created_at: "2026-09-11T00:00:00Z",
  scheduled_for: "2026-09-11T01:00:00Z",
  started_at: null,
  completed_at: null,
  result: null,
  error: null,
})

describe("news recovery policy", () => {
  it("retries safe transient GET failures exactly twice with 2/5 second delays", () => {
    for (const error of [
      new TypeError("network"),
      new ApiError(502, "req", "gateway"),
      new ApiError(503, null, "unavailable"),
      new ApiError(504, null, "timeout"),
    ]) {
      expect(retryNewsAdminGet(0, error)).toBe(true)
      expect(retryNewsAdminGet(1, error)).toBe(true)
      expect(retryNewsAdminGet(2, error)).toBe(false)
    }
    for (const status of [401, 403, 404, 409, 422])
      expect(retryNewsAdminGet(0, new ApiError(status, null, "error"))).toBe(
        false
      )
    expect([newsAdminRetryDelay(0), newsAdminRetryDelay(1)]).toEqual([
      2000, 5000,
    ])
  })
  it("distinguishes scheduled, waiting for worker, expired, and no heartbeat", () => {
    expect(newsRunState(run, Date.parse("2026-09-11T00:30:00Z"))).toBe(
      "scheduled"
    )
    expect(newsRunState(run, Date.parse("2026-09-11T01:30:00Z"))).toBe(
      "waiting_worker"
    )
    expect(
      newsRunState(
        { ...run, status: "running", heartbeat_at: "2026-09-11T01:00:00Z" },
        Date.parse("2026-09-11T01:01:31Z")
      )
    ).toBe("unresponsive")
    expect(
      newsRunState({
        ...run,
        status: "cancelled",
        error: "news_window_expired",
      })
    ).toBe("expired")
  })
  it("allows expired work, but not cancelled work or past-day checkpoints, to resume", () => {
    expect(
      canResumeNews(
        { ...run, status: "cancelled", error: "news_window_expired" },
        run.edition_date
      )
    ).toBe(true)
    expect(
      canResumeNews({ ...run, status: "cancelled" }, run.edition_date)
    ).toBe(false)
    expect(canResumeNews({ ...run, status: "failed" }, "2026-09-12")).toBe(
      false
    )
  })
})
