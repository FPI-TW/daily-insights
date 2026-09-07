import { describe, expect, it } from "vitest"
import {
  indexHistoryOutcomes,
  indexChartSymbolsForMarket,
  indexMovingAverageOutcomes,
  trackedSymbolsForMarket,
  twoYearTaipeiRange,
} from "./indices"
import {
  indexSymbolSchema,
  type IndexMovingAverages,
} from "@daily-insights/api-client"

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
      ["^DJI", "^GSPC", "^NDX"],
      [
        { status: "fulfilled", value: [] },
        { status: "rejected", reason: new Error("unavailable") },
        {
          status: "fulfilled",
          value: [
            {
              symbol: "^NDX",
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

    expect(result.series.map(item => item.symbol)).toEqual(["^NDX"])
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
      "^NDX",
      "^RUT",
      "^SOX",
      "^VIX",
    ])
    expect(trackedSymbolsForMarket("tw_equity")).toEqual(["^TWII"])
  })

  it("fans out without requiring an organization-backed market list", () => {
    expect(trackedSymbolsForMarket("us_equity")).toHaveLength(6)
  })

  it("keeps VIX in the contract but out of the general index chart", () => {
    expect(indexSymbolSchema.parse("^VIX")).toBe("^VIX")
    expect(indexChartSymbolsForMarket("us_equity")).toEqual([
      "^DJI",
      "^GSPC",
      "^NDX",
      "^RUT",
      "^SOX",
    ])
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
