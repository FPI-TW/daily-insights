import { isNotFound } from "@tanstack/react-router"
import { beforeEach, describe, expect, it, vi } from "vitest"

const getReportDetail = vi.fn()
const getMarketNews = vi.fn()
const getTodayAnalystViewpoints = vi.fn()

vi.mock("#/lib/reports", () => ({ getReportDetail }))
vi.mock("#/lib/news", () => ({ getMarketNews }))
vi.mock("#/lib/analyst-viewpoints", () => ({ getTodayAnalystViewpoints }))

const { loadMarketPage } = await import("./$marketCode")

const news = {
  market_code: "us_equity",
  target_items: 8,
  edition_date: "2026-09-02",
  revision: 1,
  status: "complete",
  locale: "en",
  generated_at: "2026-09-02T00:00:00+00:00",
  caveat: null,
  items: [],
}
const report = { kind: "report", report: { marketCode: "us_equity" } }

const viewpoint = {
  viewpoint_date: "2026-09-02",
  market_code: "us_equity",
  source_market_code: "us_stocks",
  points: ["Stocks rose on rate-cut hopes."],
  fetched_at: "2026-09-02T08:00:00+08:00",
}

describe("market report loader", () => {
  beforeEach(() => {
    getTodayAnalystViewpoints.mockReset()
    getTodayAnalystViewpoints.mockResolvedValue([])
  })

  it("loads the report, market news and the market's viewpoint", async () => {
    getReportDetail.mockResolvedValueOnce(report)
    getMarketNews.mockResolvedValueOnce(news)
    getTodayAnalystViewpoints.mockResolvedValueOnce([
      { ...viewpoint, market_code: "tw_equity" },
      viewpoint,
    ])

    await expect(
      loadMarketPage({
        params: { marketCode: "us_equity" },
        context: { locale: "en" },
      })
    ).resolves.toEqual({
      report,
      news: { marketCode: "us_equity", latest: news },
      viewpoint,
    })
    expect(getMarketNews).toHaveBeenCalledWith({
      data: { locale: "en", marketCode: "us_equity" },
    })
  })

  it("keeps the report when market news fails", async () => {
    getReportDetail.mockResolvedValueOnce(report)
    getMarketNews.mockRejectedValueOnce(new Error("news down"))

    await expect(
      loadMarketPage({
        params: { marketCode: "us_equity" },
        context: { locale: "zh-hant" },
      })
    ).resolves.toEqual({
      report,
      news: { marketCode: "us_equity", latest: null },
      viewpoint: null,
    })
  })

  it("shows the not-launched Taiwan page with Taiwan news and skips news elsewhere", async () => {
    getReportDetail.mockResolvedValueOnce({
      kind: "not-launched",
      marketCode: "tw_equity",
    })
    getMarketNews.mockResolvedValueOnce({ ...news, market_code: "tw_equity" })
    await expect(
      loadMarketPage({
        params: { marketCode: "tw_equity" },
        context: { locale: "zh-hant" },
      })
    ).resolves.toMatchObject({
      report: { kind: "not-launched", marketCode: "tw_equity" },
      news: { marketCode: "tw_equity", latest: { market_code: "tw_equity" } },
    })

    getMarketNews.mockClear()
    getReportDetail.mockResolvedValueOnce({
      kind: "not-generated",
      marketCode: "crypto",
    })
    await expect(
      loadMarketPage({
        params: { marketCode: "crypto" },
        context: { locale: "en" },
      })
    ).resolves.toEqual({
      report: { kind: "not-generated", marketCode: "crypto" },
      news: null,
      viewpoint: null,
    })
    expect(getMarketNews).not.toHaveBeenCalled()
  })

  it("throws notFound for unknown markets", async () => {
    getReportDetail.mockResolvedValueOnce({ kind: "not-found" })
    await expect(
      loadMarketPage({
        params: { marketCode: "tw_index_derivatives" },
        context: { locale: "en" },
      })
    ).rejects.toSatisfy(isNotFound)
  })
})
