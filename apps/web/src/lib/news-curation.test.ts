import { ApiError, type NewsAdminCandidate } from "@daily-insights/api-client"
import { describe, expect, it } from "vitest"
import {
  activePublishRunIds,
  candidateFilterOf,
  filterCandidates,
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
    const run = (id: string, operation: string, status: string) =>
      ({ id, operation, status }) as never
    expect(
      activePublishRunIds([
        run("a", "news_publish", "running"),
        run("b", "news_publish", "succeeded"),
        run("c", "news_all", "pending"),
      ])
    ).toEqual(new Set(["a"]))
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
