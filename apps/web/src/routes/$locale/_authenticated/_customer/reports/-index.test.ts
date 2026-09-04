import { describe, expect, it, vi } from "vitest"

const getReportList = vi.fn()
const getReportDetail = vi.fn()
const getLatestNews = vi.fn()
const getTodayAnalystViewpoints = vi.fn()

vi.mock("#/lib/reports", () => ({ getReportList, getReportDetail }))
vi.mock("#/lib/news", () => ({ getLatestNews }))
vi.mock("#/lib/analyst-viewpoints", () => ({ getTodayAnalystViewpoints }))

const { loadReportsAndNews } = await import("./index")

const reports = [{ marketCode: "crypto" }, { marketCode: "us_equity" }]
const cryptoDetail = { kind: "report", report: { marketCode: "crypto" } }
const news = {
  market_code: "global",
  target_items: 5,
  edition_date: "2026-09-02",
  revision: 1,
  status: "complete",
  locale: "en",
  generated_at: "2026-09-02T00:00:00+00:00",
  caveat: null,
  items: [],
}

describe("reports index loader", () => {
  it("returns both payloads and headline details per report", async () => {
    getReportList.mockResolvedValueOnce(reports)
    getReportDetail
      .mockResolvedValueOnce(cryptoDetail)
      .mockRejectedValueOnce(new Error("detail down"))
    getLatestNews.mockResolvedValueOnce(news)
    getTodayAnalystViewpoints.mockResolvedValueOnce([])

    await expect(
      loadReportsAndNews({ context: { locale: "en" } })
    ).resolves.toEqual({
      reports,
      overview: [
        { summary: reports[0], detail: cryptoDetail.report },
        { summary: reports[1], detail: null },
      ],
      news,
      viewpoints: [],
    })
    expect(getReportList).toHaveBeenCalledWith({ data: "en" })
    expect(getReportDetail).toHaveBeenCalledWith({
      data: { marketCode: "crypto", locale: "en" },
    })
    expect(getLatestNews).toHaveBeenCalledWith({ data: "en" })
    expect(getTodayAnalystViewpoints).toHaveBeenCalledWith()
  })

  it("keeps the report list when only the news request fails", async () => {
    getReportList.mockResolvedValueOnce(reports)
    getReportDetail.mockResolvedValue({
      kind: "not-generated",
      marketCode: "x",
    })
    getLatestNews.mockRejectedValueOnce(new Error("news API down"))
    getTodayAnalystViewpoints.mockResolvedValueOnce([])

    await expect(
      loadReportsAndNews({ context: { locale: "zh-hant" } })
    ).resolves.toMatchObject({ reports, news: null, viewpoints: [] })
  })

  it("still fails the route when the report request fails", async () => {
    const failure = new Error("reports API down")
    getReportList.mockRejectedValueOnce(failure)
    getLatestNews.mockResolvedValueOnce(news)
    getTodayAnalystViewpoints.mockResolvedValueOnce([])

    await expect(
      loadReportsAndNews({ context: { locale: "en" } })
    ).rejects.toBe(failure)
  })
})
