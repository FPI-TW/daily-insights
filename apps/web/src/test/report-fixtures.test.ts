import { describe, expect, it } from "vitest"
import {
  isNewsMarketCode,
  launchMarketCodes,
  navMarketCodes,
  newsMarketCodes,
} from "#/lib/provisional-reports"
import {
  getProvisionalReport,
  getProvisionalReportList,
} from "./report-fixtures"

describe("provisional reports adapter", () => {
  it("returns exactly the approved markets in fixed order", async () => {
    expect(
      (await getProvisionalReportList()).map(report => report.marketCode)
    ).toEqual(launchMarketCodes)
  })

  it("keeps null series points as gaps and exposes each block kind", async () => {
    const crypto = await getProvisionalReport("crypto")
    expect(crypto?.status).toBe("partial")
    const performance = crypto?.blocks.find(block => block.kind === "series")
    expect(performance?.kind).toBe("series")
    if (performance?.kind === "series")
      expect(performance.series[0]?.points[2]?.value).toBeNull()
    expect(
      (await getProvisionalReport("tw_equity"))?.blocks.map(block => block.kind)
    ).toEqual(["metric", "table", "table", "series", "metric"])
  })

  it("returns undefined for non-approved market codes", async () => {
    await expect(
      getProvisionalReport("excluded_market")
    ).resolves.toBeUndefined()
  })

  it("navigates the launch markets plus Taiwan, and publishes news for Taiwan and US", () => {
    expect(navMarketCodes).toEqual([...launchMarketCodes, "tw_equity"])
    expect(newsMarketCodes).toEqual(["tw_equity", "us_equity"])
    expect(isNewsMarketCode("tw_equity")).toBe(true)
    expect(isNewsMarketCode("crypto")).toBe(false)
    expect(isNewsMarketCode("tw_index_derivatives")).toBe(false)
  })
})
