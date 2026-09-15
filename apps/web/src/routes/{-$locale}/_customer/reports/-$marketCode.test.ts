import { isNotFound, isRedirect } from "@tanstack/react-router"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const getVisibleMarkets = vi.fn().mockResolvedValue([])
const getReportDetail = vi.fn()
const getMarketNews = vi.fn()
const getTodayAnalystViewpoints = vi.fn()
const getMarketIndexHistory = vi.fn()
const getMarketIndexMovingAverages = vi.fn()
const getTaiwanInstitutionalFlows = vi.fn()
const getTaiwanInstitutionalStocks = vi.fn()
const getVixHistory = vi.fn()
const indexRange = { start: "2024-09-02", end: "2026-09-02" }

vi.mock("#/lib/macro-dashboard.functions", () => ({
  getMacroDashboard: vi.fn().mockResolvedValue(null),
}))
vi.mock("#/lib/markets", () => ({
  getVisibleMarkets,
}))

vi.mock("#/lib/reports", () => ({ getReportDetail }))
vi.mock("#/lib/news", () => ({ getMarketNews }))
vi.mock("#/lib/analyst-viewpoints", () => ({ getTodayAnalystViewpoints }))
vi.mock("#/lib/indices", () => ({
  chartMarketCodes: ["us_equity", "tw_equity"],
  getMarketIndexHistory,
  getMarketIndexMovingAverages,
  getVixHistory,
  twoYearTaipeiRange: () => indexRange,
}))
vi.mock("#/lib/institutional-flows", () => ({
  getTaiwanInstitutionalFlows,
  getTaiwanInstitutionalStocks,
  institutionalFlowRange: (end: string) => ({
    start: "2026-05-25",
    end,
  }),
}))

const {
  INDEX_HISTORY_DEADLINE_MS,
  INDEX_MOVING_AVERAGES_DEADLINE_MS,
  VIX_HISTORY_DEADLINE_MS,
  loadMarketPage,
} = await import("./$marketCode")

const news = {
  market_code: "us_equity",
  target_items: 5,
  edition_date: "2026-09-02",
  revision: 1,
  status: "complete",
  locale: "en",
  generated_at: "2026-09-02T00:00:00+00:00",
  caveat: null,
  items: [],
}
const report = { kind: "report", report: { marketCode: "us_equity" } }
const indexHistory = {
  marketCode: "us_equity",
  start: "2024-09-02",
  end: "2026-09-02",
  series: [],
  failedSymbols: [],
}
const vixHistory = {
  symbol: "^VIX",
  start: "2024-09-02",
  end: "2026-09-02",
  bars: [],
}

const viewpoint = {
  viewpoint_date: "2026-09-02",
  market_code: "us_equity",
  source_market_code: "us_stocks",
  points: ["Stocks rose on rate-cut hopes."],
  fetched_at: "2026-09-02T08:00:00+08:00",
}

describe("market report loader", () => {
  beforeEach(() => {
    getTodayAnalystViewpoints.mockResolvedValue([])
    getMarketIndexMovingAverages.mockResolvedValue({})
    getTaiwanInstitutionalFlows.mockResolvedValue({ as_of: null, series: [] })
    getTaiwanInstitutionalStocks.mockResolvedValue({ as_of: null, rows: [] })
    getVixHistory.mockResolvedValue(vixHistory)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it("loads the report, market news and the market's viewpoint", async () => {
    getReportDetail.mockResolvedValueOnce(report)
    getMarketNews.mockResolvedValueOnce(news)
    getMarketIndexHistory.mockResolvedValueOnce(indexHistory)
    getTodayAnalystViewpoints.mockResolvedValueOnce([
      { ...viewpoint, market_code: "tw_equity" },
      viewpoint,
    ])

    const page = await loadMarketPage({
      params: { marketCode: "us_equity" },
      context: { locale: "en" },
    })
    expect(page).toMatchObject({
      report,
      news: { marketCode: "us_equity", latest: news },
      viewpoint,
    })
    if (!page.indexHistory) throw new Error("expected a deferred index chart")
    await expect(page.indexHistory).resolves.toEqual(indexHistory)
    if (!page.vixHistory) throw new Error("expected deferred VIX history")
    await expect(page.vixHistory).resolves.toEqual(vixHistory)
    expect(getMarketNews).toHaveBeenCalledWith({
      data: { locale: "en", marketCode: "us_equity" },
    })
    expect(getMarketIndexHistory).toHaveBeenCalledWith({
      data: { marketCode: "us_equity", range: indexRange },
    })
    expect(getMarketIndexMovingAverages).toHaveBeenCalledWith({
      data: { marketCode: "us_equity", range: indexRange },
    })
    expect(getVixHistory).toHaveBeenCalledWith({ data: { range: indexRange } })
  })

  it("keeps the report when market news fails", async () => {
    getReportDetail.mockResolvedValueOnce(report)
    getMarketNews.mockRejectedValueOnce(new Error("news down"))
    getMarketIndexHistory.mockRejectedValueOnce(new Error("indices down"))
    getVixHistory.mockRejectedValueOnce(new Error("VIX down"))

    const page = await loadMarketPage({
      params: { marketCode: "us_equity" },
      context: { locale: "zh-hant" },
    })
    expect(page).toMatchObject({
      report,
      news: { marketCode: "us_equity", latest: null },
      viewpoint: null,
    })
    if (!page.indexHistory) throw new Error("expected a deferred index chart")
    await expect(page.indexHistory).resolves.toBeNull()
    if (!page.vixHistory) throw new Error("expected deferred VIX history")
    await expect(page.vixHistory).resolves.toBeNull()
  })

  it("shows the not-launched Taiwan page with Taiwan news and skips news elsewhere", async () => {
    getReportDetail.mockResolvedValueOnce({
      kind: "not-launched",
      marketCode: "tw_equity",
    })
    getMarketNews.mockResolvedValueOnce({ ...news, market_code: "tw_equity" })
    getMarketIndexHistory.mockResolvedValueOnce({
      ...indexHistory,
      marketCode: "tw_equity",
    })
    const taiwanPage = await loadMarketPage({
      params: { marketCode: "tw_equity" },
      context: { locale: "zh-hant" },
    })
    expect(taiwanPage).toMatchObject({
      report: { kind: "not-launched", marketCode: "tw_equity" },
      news: { marketCode: "tw_equity", latest: { market_code: "tw_equity" } },
    })

    if (!taiwanPage.institutionalData) {
      throw new Error("expected deferred institutional data")
    }
    await expect(taiwanPage.institutionalData).resolves.toMatchObject({
      flows: { series: [] },
      stocks: { rows: [] },
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
      macroDashboard: null,
      indexHistory: null,
      indexMovingAverages: null,
      institutionalData: null,
      vixHistory: null,
    })
    expect(getMarketNews).not.toHaveBeenCalled()
    expect(getVixHistory).not.toHaveBeenCalled()
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

  it("does not wait for a stalled chart before returning report and news", async () => {
    getReportDetail.mockResolvedValueOnce(report)
    getMarketNews.mockResolvedValueOnce(news)
    getMarketIndexHistory.mockImplementationOnce(
      () => new Promise<never>(() => {})
    )

    const page = await loadMarketPage({
      params: { marketCode: "us_equity" },
      context: { locale: "en" },
    })

    expect(page.report).toEqual(report)
    expect(page.news).toEqual({ marketCode: "us_equity", latest: news })
    expect(page.indexHistory).toBeInstanceOf(Promise)
  })

  it("turns a chart deadline into local unavailable data", async () => {
    vi.useFakeTimers()
    getReportDetail.mockResolvedValueOnce(report)
    getMarketNews.mockResolvedValueOnce(news)
    getMarketIndexHistory.mockImplementationOnce(
      () => new Promise<never>(() => {})
    )

    const page = await loadMarketPage({
      params: { marketCode: "us_equity" },
      context: { locale: "en" },
    })
    if (!page.indexHistory) throw new Error("expected a deferred index chart")
    await vi.advanceTimersByTimeAsync(INDEX_HISTORY_DEADLINE_MS)

    await expect(page.indexHistory).resolves.toBeNull()
  })

  it("turns a stalled VIX request into local unavailable data", async () => {
    vi.useFakeTimers()
    getReportDetail.mockResolvedValueOnce(report)
    getMarketNews.mockResolvedValueOnce(news)
    getMarketIndexHistory.mockResolvedValueOnce(indexHistory)
    getVixHistory.mockImplementationOnce(() => new Promise<never>(() => {}))

    const page = await loadMarketPage({
      params: { marketCode: "us_equity" },
      context: { locale: "en" },
    })
    if (!page.vixHistory) throw new Error("expected deferred VIX history")
    await vi.advanceTimersByTimeAsync(VIX_HISTORY_DEADLINE_MS)

    await expect(page.vixHistory).resolves.toBeNull()
    expect(page.report).toEqual(report)
    expect(page.news).toEqual({ marketCode: "us_equity", latest: news })
  })

  it("keeps close history when moving averages reject", async () => {
    getReportDetail.mockResolvedValueOnce(report)
    getMarketNews.mockResolvedValueOnce(news)
    getMarketIndexHistory.mockResolvedValueOnce(indexHistory)
    getMarketIndexMovingAverages.mockRejectedValueOnce(new Error("ma down"))

    const page = await loadMarketPage({
      params: { marketCode: "us_equity" },
      context: { locale: "en" },
    })
    if (!page.indexHistory || !page.indexMovingAverages) {
      throw new Error("expected deferred chart data")
    }
    await expect(page.indexHistory).resolves.toEqual(indexHistory)
    await expect(page.indexMovingAverages).resolves.toEqual({})
  })

  it("does not delay report or close history for a stalled moving-average request", async () => {
    vi.useFakeTimers()
    getReportDetail.mockResolvedValueOnce(report)
    getMarketNews.mockResolvedValueOnce(news)
    getMarketIndexHistory.mockResolvedValueOnce(indexHistory)
    getMarketIndexMovingAverages.mockImplementationOnce(
      () => new Promise<never>(() => {})
    )

    const page = await loadMarketPage({
      params: { marketCode: "us_equity" },
      context: { locale: "en" },
    })
    expect(page.report).toEqual(report)
    if (!page.indexHistory || !page.indexMovingAverages) {
      throw new Error("expected deferred chart data")
    }
    await expect(page.indexHistory).resolves.toEqual(indexHistory)
    await vi.advanceTimersByTimeAsync(INDEX_MOVING_AVERAGES_DEADLINE_MS)
    await expect(page.indexMovingAverages).resolves.toEqual({})
  })
})

it("redirects the legacy forex URL when the merged market is visible", async () => {
  getVisibleMarkets.mockResolvedValueOnce([
    { code: "global_macro_bonds", name: "Macro" },
  ])
  try {
    await loadMarketPage({
      params: { marketCode: "forex" },
      context: { locale: "zh-hant" },
    })
    throw new Error("expected redirect")
  } catch (error) {
    expect(isRedirect(error)).toBe(true)
    if (isRedirect(error))
      expect(error.options).toMatchObject({
        params: { locale: "zh-hant", marketCode: "global_macro_bonds" },
        replace: true,
      })
  }
})
