import { describe, expect, it } from "vitest"
import {
  getProvisionalReport,
  getProvisionalReportList,
  marketCodes,
} from "./provisional-reports"

describe("provisional reports adapter", () => {
  it("returns exactly the approved markets in fixed order", async () => {
    expect(
      (await getProvisionalReportList()).map(report => report.marketCode)
    ).toEqual(marketCodes)
  })

  it("keeps null series points as gaps and exposes each block kind", async () => {
    const crypto = await getProvisionalReport("crypto")
    expect(crypto?.status).toBe("partial")
    const performance = crypto?.blocks.find(block => block.kind === "series")
    expect(performance?.kind).toBe("series")
    if (performance?.kind === "series")
      expect(performance.points[2]?.value).toBeNull()
    expect(
      (await getProvisionalReport("tw_equity"))?.blocks.map(block => block.kind)
    ).toEqual(["metric", "table", "table", "metric"])
  })

  it("returns undefined for non-approved market codes", async () => {
    await expect(
      getProvisionalReport("excluded_market")
    ).resolves.toBeUndefined()
  })
})
