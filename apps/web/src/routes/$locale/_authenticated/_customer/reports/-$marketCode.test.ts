import { isNotFound } from "@tanstack/react-router"
import { describe, expect, it, vi } from "vitest"

const getReportDetail = vi.fn()
const getMarketNews = vi.fn()

vi.mock("#/lib/reports", () => ({ getReportDetail }))
vi.mock("#/lib/news", () => ({ getMarketNews }))

const { loadMarketPage, marketPageChatContext } = await import("./$marketCode")

const news = {
  market_code: "us_equity",
  target_items: 8,
  edition_date: "2026-09-02",
  revision: 1,
  status: "complete" as const,
  locale: "en" as const,
  generated_at: "2026-09-02T00:00:00+00:00",
  caveat: null,
  items: [],
}
const report = { kind: "report", report: { marketCode: "us_equity" } }

describe("market report loader", () => {
  it("loads the report and market news for a news market", async () => {
    getReportDetail.mockResolvedValueOnce(report)
    getMarketNews.mockResolvedValueOnce(news)

    await expect(
      loadMarketPage({
        params: { marketCode: "us_equity" },
        context: { locale: "en" },
      })
    ).resolves.toEqual({
      report,
      news: { marketCode: "us_equity", latest: news },
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
    })
    expect(getMarketNews).not.toHaveBeenCalled()
  })

  it("uses global chat context when the Taiwan report is not launched", () => {
    expect(
      marketPageChatContext({
        report: { kind: "not-launched", marketCode: "tw_equity" },
        news: {
          marketCode: "tw_equity",
          latest: {
            ...news,
            edition_id: "10000000-0000-4000-8000-000000000001",
            market_code: "tw_equity",
          },
        },
      })
    ).toEqual({ kind: "global" })
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
