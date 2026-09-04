import { describe, expect, it } from "vitest"
import {
  indexHistoryOutcomes,
  trackedSymbolsForMarket,
  twoYearTaipeiRange,
} from "./indices"

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
      ["^TWII", "^TFNI"],
      [
        { status: "fulfilled", value: [] },
        { status: "fulfilled", value: [] },
      ]
    )

    expect(result.series).toEqual([])
    expect(result.failedSymbols).toEqual(["^TWII", "^TFNI"])
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
    expect(trackedSymbolsForMarket("tw_equity")).toEqual([
      "^TWII",
      "^TFNI",
      "^TPLI",
    ])
  })

  it("fans out without requiring an organization-backed market list", () => {
    expect(trackedSymbolsForMarket("us_equity")).toHaveLength(5)
  })
})
