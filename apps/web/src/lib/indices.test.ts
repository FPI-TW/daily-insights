import { describe, expect, it } from "vitest"
import {
  indexHistoryOutcomes,
  indexMovingAverageOutcomes,
  trackedSymbolsForMarket,
  twoYearTaipeiRange,
} from "./indices"
import type { IndexMovingAverages } from "@daily-insights/api-client"

describe("twoYearTaipeiRange", () => {
  it("uses an explicit two-calendar-year range", () => {
    expect(twoYearTaipeiRange({ year: 2026, month: 9, day: 4 })).toEqual({
      start: "2024-09-04",
      end: "2026-09-04",
    })
  })

  it("clamps February 29 when the start year is not a leap year", () => {
    expect(twoYearTaipeiRange({ year: 2024, month: 2, day: 29 })).toEqual({
      start: "2022-02-28",
      end: "2024-02-29",
    })
  })
})

describe("indexHistoryOutcomes", () => {
  it("keeps catalog ordering while exposing empty and failed symbols", () => {
    const result = indexHistoryOutcomes(
      ["^DJI", "^GSPC", "^IXIC"],
      [
        { status: "fulfilled", value: [] },
        { status: "rejected", reason: new Error("unavailable") },
        {
          status: "fulfilled",
          value: [
            {
              symbol: "^IXIC",
              market_code: "us_equity",
              trade_date: "2026-09-03",
              open: "1",
              high: "1",
              low: "1",
              close: "1",
              volume: null,
            },
          ],
        },
      ]
    )

    expect(result.series.map(item => item.symbol)).toEqual(["^IXIC"])
    expect(result.failedSymbols).toEqual(["^DJI", "^GSPC"])
  })

  it("makes an all-empty catalog result explicitly unavailable", () => {
    const result = indexHistoryOutcomes(
      ["^DJI", "^GSPC"],
      [
        { status: "fulfilled", value: [] },
        { status: "fulfilled", value: [] },
      ]
    )

    expect(result.series).toEqual([])
    expect(result.failedSymbols).toEqual(["^DJI", "^GSPC"])
  })
})

describe("trackedSymbolsForMarket", () => {
  it("uses catalog order even before any symbol has a stored row", () => {
    expect(trackedSymbolsForMarket("us_equity")).toEqual([
      "^DJI",
      "^GSPC",
      "^IXIC",
      "^RUT",
      "^SOX",
    ])
    expect(trackedSymbolsForMarket("tw_equity")).toEqual(["^TWII"])
  })

  it("fans out without requiring an organization-backed market list", () => {
    expect(trackedSymbolsForMarket("us_equity")).toHaveLength(5)
  })
})

describe("indexMovingAverageOutcomes", () => {
  it("keeps only successful series with at least one available value", () => {
    const response: IndexMovingAverages = {
      symbol: "^TWII",
      market_code: "tw_equity" as const,
      method: "sma" as const,
      price_field: "close" as const,
      formula_version: "sma-close-v1" as const,
      as_of: "2026-09-03",
      series: [
        {
          period: 20,
          points: [{ trade_date: "2026-09-03", value: "1.0000000000" }],
        },
        { period: 60, points: [{ trade_date: "2026-09-03", value: null }] },
        { period: 120, points: [{ trade_date: "2026-09-03", value: null }] },
        { period: 240, points: [{ trade_date: "2026-09-03", value: null }] },
      ],
    }

    expect(
      indexMovingAverageOutcomes(
        ["^TWII", "^DJI"],
        [
          { status: "fulfilled", value: response },
          { status: "rejected", reason: new Error("unavailable") },
        ]
      )
    ).toEqual({ "^TWII": response })
  })
})

describe("bias indicators", () => {
  it("joins API averages by date and leaves null or absent averages undrawn", async () => {
    const { biasSeries } = await import("./indices")
    const bars = ["2026-09-01", "2026-09-02", "2026-09-03"].map(trade_date => ({
      symbol: "^TWII",
      market_code: "tw_equity" as const,
      trade_date,
      open: null,
      high: null,
      low: null,
      close: "110",
      volume: null,
    }))
    const averages: IndexMovingAverages = {
      symbol: "^TWII",
      market_code: "tw_equity",
      method: "sma",
      price_field: "close",
      formula_version: "sma-close-v1",
      as_of: "2026-09-03",
      series: [
        {
          period: 20,
          points: [
            { trade_date: "2026-09-03", value: "100" },
            { trade_date: "2026-09-01", value: null },
          ],
        },
        { period: 60, points: [] },
        { period: 120, points: [] },
        { period: 240, points: [] },
      ],
    }
    const lines = biasSeries(bars, averages)
    expect(lines[0]!.points.slice(0, 2).map(p => p.value)).toEqual([null, null])
    expect(lines[0]!.points[2]!.value).toBeCloseTo(10)
    expect(lines[2]!.points.every(p => p.value === null)).toBe(true)
  })
  it("returns 50 for a constant non-null window and preserves unavailable current values", async () => {
    const { scaleBias } = await import("./indices")
    expect(
      scaleBias([
        { date: "2026-09-01", value: null },
        { date: "2026-09-02", value: 2 },
        { date: "2026-09-03", value: 2 },
      ]).value
    ).toBe(50)
    expect(scaleBias([]).value).toBeNull()
    expect(
      scaleBias([
        { date: "2026-09-01", value: 2 },
        { date: "2026-09-02", value: null },
      ]).value
    ).toBeNull()
  })
  it("recomputes positions when the calendar window or visible zoom changes", async () => {
    const { scaleBias, indexWindowStart, visibleBiasPoints } =
      await import("./indices")
    const points = [
      { date: "2025-02-01", value: -10 },
      { date: "2025-08-01", value: 20 },
      { date: "2026-01-01", value: 0 },
      { date: "2026-09-04", value: 10 },
    ]
    const window = (months: number) =>
      points.filter(p => p.date >= indexWindowStart("2026-09-04", months))
    expect(scaleBias(window(12)).value).toBe(100)
    expect(scaleBias(window(18)).value).toBe(50)
    expect(scaleBias(window(24)).value).toBeCloseTo(200 / 3)
    expect(scaleBias(visibleBiasPoints(points, 0, 33)).value).toBe(100)
    expect(indexWindowStart("2026-03-31", 1)).toBe("2026-02-28")
  })
})
