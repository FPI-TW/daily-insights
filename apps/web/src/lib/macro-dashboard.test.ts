import { describe, expect, it } from "vitest"
import {
  formatTaipeiTimestamp,
  periodChange,
  ratioPoints,
  recentPoints,
  macroDashboardSchema,
  type MacroHistory,
} from "./macro-dashboard"

function history(points: [string, string][]): MacroHistory {
  return {
    id: "eur_usd",
    symbol: "EURUSD=X",
    source: "Yahoo Finance",
    unit: "USD",
    status: "ok",
    points: points.map(([date, value]) => ({ date, value })),
  }
}

describe("macro dashboard calculations", () => {
  it("uses the previous trading session for day changes and calendar boundaries for weeks", () => {
    const data = history([
      ["2026-08-28", "100"],
      ["2026-09-03", "105"],
      ["2026-09-04", "110"],
    ])
    expect(periodChange(data, "day")).toBeCloseTo(4.761904)
    expect(periodChange(data, "week")).toBeCloseTo(10)
    expect(periodChange(data, "month")).toBeNull()
  })
  it("clamps month-end and leap-year comparisons", () => {
    expect(
      periodChange(
        history([
          ["2024-02-29", "100"],
          ["2024-03-31", "120"],
        ]),
        "month"
      )
    ).toBeCloseTo(20)
    expect(
      periodChange(
        history([
          ["2023-02-28", "100"],
          ["2024-02-29", "120"],
        ]),
        "year"
      )
    ).toBeCloseTo(20)
  })
  it("reports percentage-point differences in basis points and permits a zero rate", () => {
    expect(
      periodChange(
        history([
          ["2026-09-03", "3.98"],
          ["2026-09-04", "4.01"],
        ]),
        "day",
        true
      )
    ).toBeCloseTo(3)
    expect(
      periodChange(
        history([
          ["2026-09-03", "0"],
          ["2026-09-04", "0.01"],
        ]),
        "day",
        true
      )
    ).toBe(1)
    expect(
      periodChange(
        history([
          ["2026-09-03", "0"],
          ["2026-09-04", "0.01"],
        ]),
        "day"
      )
    ).toBeNull()
  })
  it("does not fill long gaps or divide prices from different dates", () => {
    expect(
      periodChange(
        history([
          ["2026-08-01", "100"],
          ["2026-09-04", "110"],
        ]),
        "week"
      )
    ).toBeNull()
    expect(
      ratioPoints(
        history([
          ["2026-09-03", "60"],
          ["2026-09-04", "80"],
        ]),
        history([
          ["2026-09-02", "2000"],
          ["2026-09-04", "2500"],
        ])
      )
    ).toEqual([{ date: "2026-09-04", value: 0.032 }])
    expect(
      ratioPoints(
        history([["2026-09-04", "80"]]),
        history([["2026-09-04", "0"]])
      )
    ).toEqual([])
  })
  it("selects calendar days relative to the latest available close", () => {
    expect(
      recentPoints(
        history([
          ["2026-08-01", "100"],
          ["2026-08-31", "105"],
          ["2026-09-04", "110"],
        ]),
        30
      )
    ).toHaveLength(2)
    expect(recentPoints(undefined, 90)).toEqual([])
  })
  it("rejects non-finite untrusted numeric values", () => {
    expect(
      macroDashboardSchema.safeParse({
        fetched_at: "2026-09-04T00:00:00Z",
        histories: [history([["2026-09-04", "Infinity"]])],
        calendar: { date: "2026-09-04", status: "ok", events: [] },
      }).success
    ).toBe(false)
  })
})

it("formats Taipei timestamps consistently across server and browser Intl separators", () => {
  expect(formatTaipeiTimestamp("2026-09-04T00:00:00Z")).toBe("2026-09-04 08:00")
})
