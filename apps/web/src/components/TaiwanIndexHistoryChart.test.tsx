import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import type { IndexMovingAverageMap, MarketIndexHistory } from "#/lib/indices"
import {
  TaiwanIndexHistoryChart,
  TaiwanIndexHistoryLoading,
} from "./TaiwanIndexHistoryChart"

vi.mock("@tanstack/react-router", () => ({
  ClientOnly: ({ children }: { children: React.ReactNode }) => children,
}))
vi.mock("echarts-for-react", () => ({
  default: ({
    option,
    onEvents,
  }: {
    option: { aria?: { description?: string } }
    onEvents?: { datazoom: (event: unknown) => void }
  }) => (
    <div data-testid="index-chart">
      {JSON.stringify(option)}
      {onEvents ? (
        <button
          onClick={() => onEvents.datazoom({ batch: [{ start: 0, end: 50 }] })}
        >
          Zoom {option.aria?.description}
        </button>
      ) : null}
    </div>
  ),
}))
afterEach(cleanup)
const dates = ["2025-02-01", "2025-08-01", "2026-01-01", "2026-09-04"]
const history: MarketIndexHistory = {
  marketCode: "tw_equity",
  start: "2024-09-04",
  end: "2026-09-04",
  failedSymbols: [],
  series: [
    {
      symbol: "^TWII",
      bars: dates.map((trade_date, i) => ({
        symbol: "^TWII",
        market_code: "tw_equity",
        trade_date,
        open: "100",
        high: "130",
        low: "80",
        close: String([90, 120, 100, 110][i]),
        volume: 100_000_000,
      })),
    },
  ],
}
const averages: IndexMovingAverageMap = {
  "^TWII": {
    symbol: "^TWII",
    market_code: "tw_equity",
    method: "sma",
    price_field: "close",
    formula_version: "sma-close-v1",
    as_of: "2026-09-04",
    series: [
      {
        period: 20,
        points: dates.map(trade_date => ({ trade_date, value: "100" })),
      },
      { period: 60, points: [] },
      { period: 120, points: [] },
      { period: 240, points: [] },
    ],
  },
}
function show(ui: React.ReactNode) {
  return render(<I18nextProvider i18n={createI18n("en")}>{ui}</I18nextProvider>)
}
function candles() {
  return screen
    .getByRole("heading", { name: "Taiwan Weighted Index" })
    .closest("section")!
}

describe("TaiwanIndexHistoryChart", () => {
  it("updates scaled readings for window chips and ECharts dataZoom without refetching", async () => {
    show(
      <TaiwanIndexHistoryChart
        history={history}
        locale="en"
        movingAverages={Promise.resolve(averages)}
      />
    )
    await waitFor(() =>
      expect(screen.getByRole("meter", { name: "20MA bias" })).toHaveAttribute(
        "aria-valuenow",
        expect.stringMatching(/^50/)
      )
    )
    expect(screen.queryByRole("combobox")).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Last 1 year" }))
    expect(screen.getByRole("meter", { name: "20MA bias" })).toHaveAttribute(
      "aria-valuenow",
      "100"
    )
    // Two years is not offered: the stored history is still too short for it.
    expect(
      screen.queryByRole("button", { name: "Last 2 years" })
    ).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Last 1.5 years" }))
    expect(screen.getByRole("meter", { name: "20MA bias" })).toHaveAttribute(
      "aria-valuenow",
      expect.stringMatching(/^50/)
    )
    // The first half of the 1.5-year window ends on the 0% reading.
    fireEvent.click(screen.getByRole("button", { name: "Zoom TAIEX bias" }))
    expect(screen.getByRole("meter", { name: "20MA bias" })).toHaveAttribute(
      "aria-valuenow",
      "0"
    )
    expect(within(candles()).getByTestId("index-chart")).toHaveTextContent(
      '"type":"candlestick"'
    )
    expect(within(candles()).getByTestId("index-chart")).toHaveTextContent(
      '"xAxisIndex":[0,1]'
    )
    expect(
      within(candles()).queryByText("Last 2 years · daily OHLC")
    ).toBeNull()
    expect(within(candles()).queryByText("Visible date range")).toBeNull()
    fireEvent.click(
      within(candles()).getByRole("button", {
        name: "Zoom Taiwan Weighted Index",
      })
    )
    expect(within(candles()).queryByText("2025-02-01 – 2026-01-01")).toBeNull()
    expect(within(candles()).getByTestId("index-chart")).toHaveTextContent(
      '"height":26'
    )
  })
  it("keeps the initial averages request pending while showing available candles", async () => {
    let resolve!: (value: IndexMovingAverageMap) => void
    const promise = new Promise<IndexMovingAverageMap>(done => {
      resolve = done
    })
    show(
      <TaiwanIndexHistoryChart
        history={history}
        locale="en"
        movingAverages={promise}
      />
    )
    expect(screen.getByText("Loading moving averages…")).toBeVisible()
    expect(screen.queryByText(/currently unavailable/)).toBeNull()
    expect(within(candles()).getByTestId("index-chart")).toBeInTheDocument()
    resolve(averages)
    await waitFor(() =>
      expect(
        screen.getByRole("meter", { name: "20MA bias" })
      ).toBeInTheDocument()
    )
    expect(
      screen.getByRole("meter", { name: "120MA bias" })
    ).not.toHaveAttribute("aria-valuenow")
  })
  it("omits missing OHLC rather than filling it with close and preserves null volume", () => {
    const bars = history.series[0]!.bars.map((bar, i) =>
      i === 0 ? { ...bar, open: null, volume: null } : bar
    )
    show(
      <TaiwanIndexHistoryChart
        history={{ ...history, series: [{ symbol: "^TWII", bars }] }}
        locale="en"
      />
    )
    expect(screen.getByText(/1 sessions have incomplete OHLC/)).toBeVisible()
    const chart = within(candles()).getByTestId("index-chart")
    expect(chart).toHaveTextContent(
      '"data":[[null,null,null,null],[100,120,80,130]'
    )
    expect(chart).toHaveTextContent('"value":null')
    expect(chart).toHaveTextContent('"value":1')
    expect(screen.getAllByText("Volume (100M shares)").length).toBeGreaterThan(
      0
    )
  })
  it("preserves the US symbol selector and partial failures", () => {
    show(
      <TaiwanIndexHistoryChart
        history={{
          ...history,
          marketCode: "us_equity",
          failedSymbols: ["^SOX"],
          series: [
            { symbol: "^DJI", bars: history.series[0]!.bars },
            { symbol: "^GSPC", bars: history.series[0]!.bars },
          ],
        }}
        locale="en"
      />
    )
    expect(
      screen.getByText(/Some indices could not be loaded: \^SOX/)
    ).toBeVisible()
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "^GSPC" },
    })
    expect(
      screen.getByRole("heading", {
        name: "S&P 500 Index — daily candles + MA + volume",
      })
    ).toBeVisible()
  })
  it("has explicit loading, failure and empty states", () => {
    show(<TaiwanIndexHistoryLoading />)
    expect(screen.getByRole("status")).toHaveAccessibleName()
    cleanup()
    show(<TaiwanIndexHistoryChart history={null} locale="en" />)
    expect(screen.getByRole("status")).toHaveTextContent("unavailable")
    cleanup()
    show(
      <TaiwanIndexHistoryChart
        history={{ ...history, series: [] }}
        locale="en"
      />
    )
    expect(screen.getByRole("status")).toHaveTextContent("no index daily bars")
  })
})
