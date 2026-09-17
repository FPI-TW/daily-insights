import { ApiError, type NewsAdminCandidate } from "@daily-insights/api-client"
import { describe, expect, it } from "vitest"
import {
  activePublishRunIds,
  candidateFilterOf,
  filterCandidates,
  isNewsRun,
  newsMutationErrorKey,
  publishRunCounts,
} from "./news-curation"

const stages = [
  "discovered",
  "fetch_failed",
  "unused",
  "reviewed",
  "dropped",
  "published",
] as const

const candidates = stages.map(
  (stage, index) =>
    ({ id: `candidate-${index}`, stage }) as unknown as NewsAdminCandidate
)

describe("news curation helpers", () => {
  it("groups the six stages into the three editor filters", () => {
    expect(stages.map(candidateFilterOf)).toEqual([
      "other",
      "other",
      "other",
      "reviewed",
      "dropped",
      "other",
    ])
    expect(filterCandidates(candidates, "all")).toHaveLength(6)
    expect(filterCandidates(candidates, "dropped").map(c => c.stage)).toEqual([
      "dropped",
    ])
    expect(filterCandidates(candidates, "other")).toHaveLength(4)
  })

  it("collects only active manual publish runs", () => {
    const run = (id: string, job_key: string, status: string) =>
      ({ id, job_key, status }) as never
    expect(
      activePublishRunIds([
        run("a", "news_publish_job", "running"),
        run("b", "news_publish_job", "succeeded"),
        run("c", "news_daily_update", "pending"),
      ])
    ).toEqual(new Set(["a"]))
  })

  it("includes the automatic mixed internal-services job in news polling", () => {
    expect(isNewsRun({ job_key: "internal_services_daily_update" })).toBe(true)
  })

  it("maps conflict and unavailable statuses to their messages", () => {
    expect(newsMutationErrorKey(new ApiError(409, null, "busy"), "x")).toBe(
      "newsManagementConflict"
    )
    expect(newsMutationErrorKey(new ApiError(503, null, "off"), "x")).toBe(
      "newsManagementUnavailable"
    )
    expect(newsMutationErrorKey(new Error("boom"), "fallback")).toBe("fallback")
  })

  it("reads publish counts only from a well-formed run result", () => {
    expect(publishRunCounts({ published: 2, failed: 1 })).toEqual({
      published: 2,
      failed: 1,
    })
    expect(publishRunCounts({ outcome: "complete" })).toBeNull()
    expect(publishRunCounts(null)).toBeNull()
  })
})
