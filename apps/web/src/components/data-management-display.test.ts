import type { JobRun, RoutineRun } from "@daily-insights/api-client"
import { describe, expect, it } from "vitest"
import { groupRuns, orderRuns, recentRoutines } from "./data-management-display"

function run(id: string, overrides: Partial<JobRun> = {}): JobRun {
  return {
    id,
    routine_run_id: null,
    job_key: "same_refresh",
    kind: "function",
    trigger: "manual",
    edition_date: "2026-09-07",
    deadline_at: null,
    status: "pending",
    requested_by_user_id: null,
    payload: null,
    started_at: null,
    completed_at: null,
    result: null,
    error: null,
    created_at: "2026-09-07T00:00:00Z",
    functions: [],
    depends_on: [],
    downstream_jobs: [],
    ...overrides,
  }
}

describe("data management display order", () => {
  it("puts dependencies first even with equal timestamps and reversed input", () => {
    const refresh = run("z", { downstream_jobs: ["a"] })
    const publish = run("a", { kind: "projection", depends_on: ["z"] })
    expect(groupRuns([publish, refresh])[0]!.runs.map(run => run.id)).toEqual([
      "z",
      "a",
    ])
  })
  it("groups routines while ordering independent jobs by start or queue time", () => {
    const early = run("a", {
      routine_run_id: "routine",
      started_at: "2026-09-07T01:00:00Z",
    })
    const later = run("b", {
      routine_run_id: "routine",
      started_at: "2026-09-07T02:00:00Z",
    })
    const pending = run("c", {
      routine_run_id: "routine",
      created_at: "2026-09-07T03:00:00Z",
    })
    expect(
      groupRuns([pending, later, early])[0]!.runs.map(run => run.id)
    ).toEqual(["a", "b", "c"])
  })
  it("keeps separate manual executions separate and groups newest first", () => {
    expect(
      groupRuns([
        run("old"),
        run("new", { created_at: "2026-09-07T03:00:00Z" }),
      ]).map(group => group.runs.map(run => run.id))
    ).toEqual([["new"], ["old"]])
  })
  it("discloses missing links without inventing jobs and connects shared explicit links", () => {
    const groups = groupRuns([
      run("a", { depends_on: ["outside"] }),
      run("b", { depends_on: ["outside"] }),
    ])
    expect(groups).toHaveLength(1)
    expect(groups[0]!.missingIds).toEqual(["outside"])
    expect(groups[0]!.runs.map(run => run.id)).toEqual(["a", "b"])
  })
  it("retains every job even with cyclic dependency data", () => {
    expect(
      orderRuns([
        run("a", { depends_on: ["b"] }),
        run("b", { depends_on: ["a"] }),
      ]).map(run => run.id)
    ).toEqual(["a", "b"])
  })
  it("uses an inclusive five-day window across a month boundary and excludes future dates", () => {
    const items = ["2026-08-27", "2026-08-28", "2026-09-01", "2026-09-02"].map(
      edition_date =>
        ({
          edition_date,
          id: edition_date,
          created_at: `${edition_date}T00:00:00Z`,
        }) as RoutineRun
    )
    expect(
      recentRoutines(items, "2026-09-01").map(item => item.edition_date)
    ).toEqual(["2026-09-01", "2026-08-28"])
  })
})
