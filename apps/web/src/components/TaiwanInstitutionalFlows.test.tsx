import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import type { TaiwanInstitutionalData } from "#/lib/institutional-flows"
import type { MarketIndexHistory } from "#/lib/indices"
import { TaiwanInstitutionalFlows } from "./TaiwanInstitutionalFlows"

let chartOption: Record<string, unknown> | null = null
vi.mock("@tanstack/react-router", () => ({
  ClientOnly: ({ children }: { children: React.ReactNode }) => children,
}))
vi.mock("echarts-for-react", () => ({
  default: ({ option }: { option: Record<string, unknown> }) => {
    chartOption = option
    return <div data-testid="institutional-chart" />
  },
}))

afterEach(() => {
  cleanup()
  chartOption = null
})

const dates = ["2026-09-02", "2026-09-03", "2026-09-04"]
const data: TaiwanInstitutionalData = {
  flows: dates
    .map((trade_date, index) => ({
      trade_date,
      foreign: [200_000_000, -300_000_000, 400_000_000][index]!,
      trust: [100_000_000, 100_000_000, -100_000_000][index]!,
      dealer: [50_000_000, -50_000_000, 100_000_000][index]!,
    }))
    .reverse(),
  stocks: {
    trade_date: "2026-09-03",
    top_buys: [12_000, 11_000, 10_000, 9_000, 8_000].map(
      (net_shares, index) => ({
        trade_date: "2026-09-03",
        symbol: String(2300 + index),
        security_name: `Stock ${index}`,
        net_shares,
      })
    ),
    top_sells: [-6_000, -5_000, -4_000, -3_000, -1].map(
      (net_shares, index) => ({
        trade_date: "2026-09-03",
        symbol: String(2311 - index),
        security_name: `Stock ${11 - index}`,
        net_shares,
      })
    ),
  },
}
const history: MarketIndexHistory = {
  marketCode: "tw_equity",
  start: dates[0]!,
  end: dates.at(-1)!,
  failedSymbols: [],
  series: [
    {
      symbol: "^TWII",
      bars: dates.map((trade_date, index) => ({
        symbol: "^TWII",
        market_code: "tw_equity",
        trade_date,
        open: "20000",
        high: "20200",
        low: "19800",
        close: String(20000 + index * 100),
        volume: 1,
      })),
    },
  ],
}

function show(
  locale: "zh-hant" | "zh-hans" | "en" = "en",
  suppliedData = data,
  suppliedHistory: MarketIndexHistory | null = history
) {
  return render(
    <I18nextProvider i18n={createI18n(locale)}>
      <TaiwanInstitutionalFlows
        data={suppliedData}
        history={suppliedHistory}
        locale={locale}
      />
    </I18nextProvider>
  )
}

describe("TaiwanInstitutionalFlows", () => {
  it("renders real rows, top-five rankings and one selected chip per control", () => {
    show()
    expect(
      screen.getByRole("heading", { name: "Institutional flows" })
    ).toBeInTheDocument()
    expect(screen.getAllByRole("button", { pressed: true })).toHaveLength(3)
    expect(screen.getByText("Stock 0")).toBeInTheDocument()
    expect(screen.queryByText("Stock 5")).not.toBeInTheDocument()
    expect(screen.getByText("Stock 11")).toBeInTheDocument()
    expect(screen.getByText("-0.001")).toBeInTheDocument()
    expect(screen.queryByRole("columnheader", { name: "Foreign" })).toBeNull()
    expect(
      screen.getByText(/Market as of 2026-09-04 · Stocks as of 2026-09-03/)
    ).toBeInTheDocument()
    expect(
      screen.queryByText("Figures are layout placeholders pending the T86 feed")
    ).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Foreign" }))
    expect(screen.getByRole("button", { name: "Foreign" })).toHaveAttribute(
      "aria-pressed",
      "true"
    )
    expect(screen.getByRole("button", { name: "All three" })).toHaveAttribute(
      "aria-pressed",
      "false"
    )
  })

  it("sorts the API's descending dates and converts TWD to NT$100M before accumulation", () => {
    show()
    expect(chartOption?.xAxis).toMatchObject({ data: dates })
    expect(chartOption?.series).toMatchObject([
      { data: [3.5, null, 4] },
      { data: [null, -2.5, null] },
      { type: "line" },
    ])
    fireEvent.click(screen.getByRole("button", { name: "Cumulative" }))
    expect(chartOption?.series).toMatchObject([
      { data: [3.5, 1, 5] },
      { data: [null, null, null] },
      { type: "line" },
    ])
    fireEvent.click(screen.getByRole("button", { name: "Foreign" }))
    expect(chartOption?.series).toMatchObject([
      { data: [2, null, 3] },
      { data: [null, -1, null] },
      { type: "line" },
    ])
  })

  it("keeps institutional values visible when index history is unavailable", () => {
    show("en", data, null)
    expect(screen.getByTestId("institutional-chart")).toBeInTheDocument()
    expect(chartOption?.series).toMatchObject([
      { data: [3.5, null, 4] },
      {},
      { data: [null, null, null] },
    ])
  })

  it("distinguishes a successful empty result from an unavailable API", () => {
    show("en", {
      flows: [],
      stocks: { trade_date: null, top_buys: [], top_sells: [] },
    })
    expect(
      screen.getByText("No stored institutional flows for this period.")
    ).toBeInTheDocument()
    expect(
      screen.getByText("No stock flow rankings are available yet.")
    ).toBeInTheDocument()
    cleanup()
    show("en", { flows: null, stocks: null })
    expect(
      screen.queryByText("No stored institutional flows for this period.")
    ).toBeNull()
    expect(screen.getAllByRole("status")).toHaveLength(2)
  })

  it("aligns both axes to six intervals and keeps zero on the left grid", () => {
    show()
    expect(screen.getByTestId("institutional-chart")).toBeInTheDocument()
    const axes = chartOption?.yAxis as Array<Record<string, number>>
    expect(axes[0]?.splitNumber).toBe(6)
    expect(axes[0]?.min).toBe(-(axes[0]?.max ?? 0))
    expect(
      ((axes[1]?.max ?? 0) - (axes[1]?.min ?? 0)) / (axes[1]?.interval ?? 1)
    ).toBeCloseTo(6)
  })

  it.each([
    ["zh-hant", "三大法人"],
    ["zh-hans", "三大法人"],
    ["en", "Institutional flows"],
  ] as const)("has localized section text for %s", (locale, heading) => {
    show(locale)
    expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument()
  })
})
