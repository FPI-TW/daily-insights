import { describe, expect, it, vi } from "vitest"

const getReportList = vi.fn()
const getNewsroomEdition = vi.fn()
const getTodayAnalystViewpoints = vi.fn()

vi.mock("#/lib/reports", () => ({ getReportList }))
vi.mock("#/lib/newsroom", () => ({ getNewsroomEdition }))
vi.mock("#/lib/analyst-viewpoints", () => ({ getTodayAnalystViewpoints }))

const { loadReportsAndNews } = await import("./index")

const reports = [{ marketCode: "crypto" }, { marketCode: "us_equity" }]
const news = {
  market_code: "global",
  locale: "en",
  edition_id: "00000000-0000-4000-8000-000000000001",
  edition_date: "2026-09-02",
  is_today: true,
  published_at: "2026-09-02T01:00:00+00:00",
  items: [],
}

describe("reports index loader", () => {
  it("returns report summaries, news, and analyst viewpoints", async () => {
    getReportList.mockResolvedValueOnce(reports)
    getNewsroomEdition.mockResolvedValueOnce(news)
    getTodayAnalystViewpoints.mockResolvedValueOnce([])

    await expect(
      loadReportsAndNews({ context: { locale: "en" } })
    ).resolves.toEqual({
      reports,
      news,
      viewpoints: [],
    })
    expect(getReportList).toHaveBeenCalledWith({ data: "en" })
    expect(getNewsroomEdition).toHaveBeenCalledWith({
      data: { locale: "en", marketCode: "global" },
    })
    expect(getTodayAnalystViewpoints).toHaveBeenCalledWith()
  })

  it("keeps the report list when only the news request fails", async () => {
    getReportList.mockResolvedValueOnce(reports)
    getNewsroomEdition.mockRejectedValueOnce(new Error("news API down"))
    getTodayAnalystViewpoints.mockResolvedValueOnce([])

    await expect(
      loadReportsAndNews({ context: { locale: "zh-hant" } })
    ).resolves.toMatchObject({ reports, news: null, viewpoints: [] })
  })

  it("still fails the route when the report request fails", async () => {
    const failure = new Error("reports API down")
    getReportList.mockRejectedValueOnce(failure)
    getNewsroomEdition.mockResolvedValueOnce(news)
    getTodayAnalystViewpoints.mockResolvedValueOnce([])

    await expect(
      loadReportsAndNews({ context: { locale: "en" } })
    ).rejects.toBe(failure)
  })
})
