import { cleanup, render, screen, within } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import type { ProvisionalReport } from "#/lib/provisional-reports"

const state = vi.hoisted(() => ({
  marketCode: "us_equity",
  results: new Map<
    string,
    {
      data: unknown
      isPending: boolean
      isFetching: boolean
      error: Error | null
      refetch: ReturnType<typeof vi.fn>
    }
  >(),
  navigate: vi.fn(),
}))
vi.mock("@tanstack/react-router", async importOriginal => ({
  ...(await importOriginal<typeof import("@tanstack/react-router")>()),
  useNavigate: () => state.navigate,
  createFileRoute: () => (options: unknown) => ({
    options,
    useParams: () => ({ marketCode: state.marketCode }),
    useRouteContext: () => ({
      locale: "en",
      user: { id: "u", organization_id: "o" },
    }),
  }),
}))
vi.mock("#/lib/market-queries", () => ({
  marketQueries: () =>
    Object.fromEntries(
      [
        "markets",
        "report",
        "news",
        "viewpoints",
        "macro",
        "history",
        "averages",
        "vix",
        "vixAverages",
        "flows",
        "stocks",
      ].map(key => [key, () => ({ queryKey: [key] })])
    ),
  useMarketQuery: ({ queryKey }: { queryKey: [string] }) =>
    state.results.get(queryKey[0]),
}))
vi.mock("#/components/PageContextChat", () => ({ useChatPageContext: vi.fn() }))
vi.mock("#/components/IndexHistoryChart", () => ({
  IndexHistoryChart: () => <section>Index history</section>,
  IndexHistoryLoading: () => null,
}))
vi.mock("#/components/TaiwanIndexHistoryChart", () => ({
  TaiwanIndexHistoryChart: () => <section>Taiwan index history</section>,
  TaiwanIndexHistoryLoading: () => null,
}))
vi.mock("#/components/VixHistoryChart", () => ({
  VixHistoryChart: () => null,
  VixHistoryLoading: () => null,
}))
vi.mock("#/components/TaiwanInstitutionalFlows", () => ({
  FlowPanel: () => <section>Retained flows</section>,
  StocksPanel: () => <section>Retained stocks</section>,
  TaiwanInstitutionalFlowsLoading: () => null,
}))
const { Route } = await import("./$marketCode")
const partialReport: ProvisionalReport = {
  marketCode: "us_equity",
  status: "partial",
  editionDate: "2026-10-05",
  sourceDate: "2026-10-02",
  caveat: "Source caveat",
  caveatKey: "reportCaveatLive",
  summaryKey: "reportSummary_us_equity",
  blocks: [
    {
      kind: "table",
      status: "ok",
      titleKey: "reportBlockUsMegaCaps",
      columns: [{ labelKey: "reportColumnInstrument", unitCode: null }],
      rows: [[{ kind: "literal", value: "AAPL" }]],
    },
  ],
}
function page() {
  const ReportPage = Route.options.component
  if (!ReportPage) throw new Error("missing component")
  return (
    <I18nextProvider i18n={createI18n("en")}>
      <ReportPage />
    </I18nextProvider>
  )
}
function query(key: string) {
  const result = state.results.get(key)
  if (!result) throw new Error(`missing query ${key}`)
  return result
}
function cellContaining(text: string) {
  const cell = screen.getByText(text).parentElement
  if (!cell) throw new Error("missing grid cell")
  return cell
}
beforeEach(() => {
  state.marketCode = "us_equity"
  state.results.clear()
  for (const key of [
    "markets",
    "report",
    "news",
    "viewpoints",
    "macro",
    "history",
    "averages",
    "vix",
    "vixAverages",
    "flows",
    "stocks",
  ])
    state.results.set(key, {
      data: undefined,
      isPending: false,
      isFetching: false,
      error: null,
      refetch: vi.fn(),
    })
  query("markets").data = [{ code: "us_equity" }, { code: "tw_equity" }]
  query("report").data = { kind: "report", report: partialReport }
  query("viewpoints").data = []
  query("history").data = {
    marketCode: "us_equity",
    start: "2026-10-01",
    end: "2026-10-02",
    failedSymbols: [],
    series: [
      {
        symbol: "^GSPC",
        bars: [
          {
            symbol: "^GSPC",
            market_code: "us_equity",
            trade_date: "2026-10-02",
            close: "6000",
            open: null,
            high: null,
            low: null,
            volume: null,
            trade_value: null,
          },
        ],
      },
    ],
  }
  query("flows").data = {}
  query("stocks").data = {}
})
afterEach(cleanup)

describe("market desktop grid cells", () => {
  it("contains US partial-report notices and retained-data refresh/errors in their original columns", () => {
    const view = render(page())
    const performance = screen.getByRole("region", {
      name: "US five-index performance",
    })
    const historyCell = performance.parentElement!
    const grid = historyCell.parentElement!
    const reportCell = grid.children[1] as HTMLElement
    expect(grid).toHaveClass("xl:grid-cols-2")
    expect(Array.from(grid.children)).toEqual([historyCell, reportCell])
    expect(historyCell).toHaveClass("flex", "min-w-0", "flex-col")
    expect(performance).toHaveClass("flex-1")
    expect(performance).not.toHaveClass("h-full")
    expect(within(performance).getByText("6,000.00")).toBeVisible()
    expect(reportCell).toHaveClass("min-w-0")
    expect(within(reportCell).getByText("Source caveat")).toBeVisible()
    expect(within(reportCell).getByText("Some sections missing")).toBeVisible()
    expect(within(reportCell).getByText("AAPL")).toBeVisible()

    query("history").isFetching = true
    query("report").isFetching = true
    view.rerender(page())
    expect(within(historyCell).getByRole("status")).toBeVisible()
    expect(within(reportCell).getByRole("status")).toBeVisible()
    expect(Array.from(grid.children)).toEqual([historyCell, reportCell])

    query("history").isFetching = false
    query("history").error = new Error("offline")
    query("report").isFetching = false
    query("report").error = new Error("offline")
    view.rerender(page())
    expect(
      within(historyCell).getByRole("button", { name: "Retry" })
    ).toBeVisible()
    expect(
      within(reportCell).getByRole("button", { name: "Retry" })
    ).toBeVisible()
    expect(within(historyCell).getByText("6,000.00")).toBeVisible()
    expect(within(reportCell).getByText("AAPL")).toBeVisible()
    expect(Array.from(grid.children)).toEqual([historyCell, reportCell])
  })
  it("keeps TW stocks beside retained flows while either column refreshes or fails", () => {
    state.marketCode = "tw_equity"
    query("report").data = { kind: "not-launched", marketCode: "tw_equity" }
    const view = render(page())
    const flowCell = cellContaining("Retained flows")
    const stockCell = cellContaining("Retained stocks")
    const grid = flowCell.parentElement!
    expect(grid).toHaveClass("grid", "items-start")
    expect(flowCell).toHaveClass("min-w-0")
    expect(stockCell).toHaveClass("min-w-0")
    expect(Array.from(grid.children)).toEqual([flowCell, stockCell])

    query("flows").isFetching = true
    view.rerender(page())
    expect(within(flowCell).getByRole("status")).toBeVisible()
    expect(within(stockCell).queryByRole("status")).toBeNull()
    expect(Array.from(grid.children)).toEqual([flowCell, stockCell])

    query("flows").isFetching = false
    query("flows").error = new Error("offline")
    query("stocks").isFetching = true
    view.rerender(page())
    expect(
      within(flowCell).getByRole("button", { name: "Retry" })
    ).toBeVisible()
    expect(within(stockCell).getByRole("status")).toBeVisible()
    expect(within(flowCell).getByText("Retained flows")).toBeVisible()
    expect(within(stockCell).getByText("Retained stocks")).toBeVisible()
    expect(Array.from(grid.children)).toEqual([flowCell, stockCell])
  })
})
