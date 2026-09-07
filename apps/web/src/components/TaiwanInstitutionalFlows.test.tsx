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
  flows: {
    as_of: "2026-09-04",
    contract_version: "twse-institutional-v1",
    contract_hash: "a".repeat(64),
    endpoint: "/rwd/zh/fund/BFI82U",
    series: dates.map((trade_date, index) => ({
      trade_date,
      foreign: String([2, -3, 4][index]),
      trust: String([1, 1, -1][index]),
      dealer: String([0.5, -0.5, 1][index]),
      total: String([3.5, -2.5, 4][index]),
    })),
  },
  stocks: {
    as_of: "2026-09-04",
    contract_version: "twse-institutional-v1",
    contract_hash: "a".repeat(64),
    endpoint: "/rwd/zh/fund/T86",
    rows: Array.from({ length: 12 }, (_, index) => {
      const total = index < 6 ? 12 - index : -(index - 5)
      return {
        symbol: String(2300 + index),
        name: `Stock ${index}`,
        foreign_lots: String(total),
        trust_lots: "0",
        dealer_lots: "0",
        total_lots: String(total),
      }
    }),
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

function show(locale: "zh-hant" | "zh-hans" | "en" = "en") {
  return render(
    <I18nextProvider i18n={createI18n(locale)}>
      <TaiwanInstitutionalFlows data={data} history={history} locale={locale} />
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
