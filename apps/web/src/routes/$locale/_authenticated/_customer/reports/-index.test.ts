import { describe, expect, it, vi } from "vitest"

const getReportList = vi.fn()
const getLatestNews = vi.fn()

vi.mock("#/lib/reports", () => ({ getReportList }))
vi.mock("#/lib/news", () => ({ getLatestNews }))

const { loadReportsAndNews } = await import("./index")

const reports = [{ market_code: "crypto" }]
const news = {
  edition_date: "2026-09-02",
  revision: 1,
  status: "complete",
  locale: "en",
  generated_at: "2026-09-02T00:00:00+00:00",
  caveat: null,
  items: [],
}

describe("reports index loader", () => {
  it("returns both payloads when both requests succeed", async () => {
    getReportList.mockResolvedValueOnce(reports)
    getLatestNews.mockResolvedValueOnce(news)

    await expect(
      loadReportsAndNews({ context: { locale: "en" } })
    ).resolves.toEqual({ reports, news })
    expect(getReportList).toHaveBeenCalledWith({ data: "en" })
    expect(getLatestNews).toHaveBeenCalledWith({ data: "en" })
  })

  it("keeps the report list when only the news request fails", async () => {
    getReportList.mockResolvedValueOnce(reports)
    getLatestNews.mockRejectedValueOnce(new Error("news API down"))

    await expect(
      loadReportsAndNews({ context: { locale: "zh-hant" } })
    ).resolves.toEqual({ reports, news: null })
  })

  it("still fails the route when the report request fails", async () => {
    const failure = new Error("reports API down")
    getReportList.mockRejectedValueOnce(failure)
    getLatestNews.mockResolvedValueOnce(news)

    await expect(
      loadReportsAndNews({ context: { locale: "en" } })
    ).rejects.toBe(failure)
  })
})
