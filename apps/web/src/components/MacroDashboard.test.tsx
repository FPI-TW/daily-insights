import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import type { MacroDashboardData } from "#/lib/macro-dashboard"
import { MacroDashboard, MacroDashboardLoading } from "./MacroDashboard"

vi.mock("@tanstack/react-router", () => ({
  ClientOnly: ({ children }: { children: React.ReactNode }) => children,
}))
vi.mock("echarts-for-react", () => ({
  default: ({ option }: { option: unknown }) => (
    <div data-testid="chart">{JSON.stringify(option)}</div>
  ),
}))
afterEach(cleanup)
const data: MacroDashboardData = {
  fetched_at: "2026-09-04T00:00:00Z",
  calendar: {
    date: "2026-09-04",
    source: "",
    status: "disabled",
    events: [],
  },
  histories: ["eur_usd", "usd_jpy"].map((id, index) => ({
    id,
    symbol: id,
    unit: "USD",
    source: "Yahoo Finance",
    status: "ok",
    base_dates: { "30": "2026-09-04", "90": "2026-09-04", "365": "2026-01-02" },
    points: [
      { date: "2026-01-02", value: "100" },
      { date: "2026-09-04", value: index ? "145" : "1.17" },
    ],
  })),
}
function show(ui: React.ReactNode) {
  render(<I18nextProvider i18n={createI18n("en")}>{ui}</I18nextProvider>)
}
describe("integrated macro dashboard", () => {
  it("has an explicit initial loading state", () => {
    show(<MacroDashboardLoading />)
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading macro, bonds and foreign exchange"
    )
    expect(screen.queryByText(/temporarily unavailable/)).toBeNull()
  })
  it("switches currency pair and range without fetching another dataset", () => {
    show(<MacroDashboard data={data} locale="en" />)
    const chart = screen.getByRole("img", {
      name: "Global foreign exchange price trends · EUR/USD",
    })
    expect(chart).toHaveTextContent("1.17")
    expect(chart).not.toHaveTextContent("2026-01-02")
    fireEvent.click(
      within(screen.getByRole("group", { name: "Currency pair" })).getByRole(
        "button",
        { name: "USD/JPY" }
      )
    )
    expect(
      screen.getByRole("img", { name: /trends · USD\/JPY/ })
    ).toHaveTextContent("145")
    const fxPanel = screen
      .getByRole("heading", { name: "Global foreign exchange price trends" })
      .closest("section")!
    fireEvent.click(within(fxPanel).getByRole("button", { name: "365 days" }))
    expect(
      screen.getByRole("img", { name: /trends · USD\/JPY/ })
    ).toHaveTextContent("2026-01-02")
    expect(
      within(fxPanel).getByRole("button", { name: "365 days" })
    ).toHaveAttribute("aria-pressed", "true")
  })
  it("renders the independent Base 100 FX chart with each line's base date", () => {
    show(<MacroDashboard data={data} locale="en" />)
    const chart = screen.getByRole("img", {
      name: "Asian currency relative performance (Base 100)",
    })
    expect(chart).toHaveTextContent("100")
    expect(screen.getAllByText("Base date 2026-09-04")).not.toHaveLength(0)
  })
  it("renders the foreign exchange panels as full-width rows in reading order", () => {
    show(<MacroDashboard data={data} locale="en" />)
    const headings = [
      "US dollar index trend",
      "Asian currency relative performance (Base 100)",
      "Global foreign exchange price trends",
      "Major currency exchange rates",
    ].map(name => screen.getByRole("heading", { name }))

    for (let index = 1; index < headings.length; index += 1) {
      expect(
        headings[index - 1]!.compareDocumentPosition(headings[index]!) &
          Node.DOCUMENT_POSITION_FOLLOWING
      ).not.toBe(0)
    }
    expect(
      headings.map(heading => heading.closest("section")?.parentElement)
    ).toEqual(
      Array(headings.length).fill(
        headings[0]!.closest("section")?.parentElement
      )
    )
  })
  it("rebuilds every Base 100 line from its matching range base date", () => {
    const normalizedData: MacroDashboardData = {
      ...data,
      histories: ["usd_twd", "usd_jpy", "usd_krw", "usd_sgd", "usd_cnh"].map(
        id => ({
          id,
          symbol: id,
          unit: "USD",
          source: "Twelve Data",
          status: "ok" as const,
          base_dates: {
            "30": "2026-08-10",
            "90": "2026-08-10",
            "365": "2025-10-01",
          },
          points: [
            { date: "2025-10-01", value: "10" },
            { date: "2026-08-10", value: "20" },
            { date: "2026-09-04", value: "30" },
          ],
        })
      ),
    }
    show(<MacroDashboard data={normalizedData} locale="en" />)
    const panel = screen
      .getByRole("heading", {
        name: "Asian currency relative performance (Base 100)",
      })
      .closest("section")!
    expect(within(panel).getAllByText("Base date 2026-08-10")).toHaveLength(5)

    fireEvent.click(within(panel).getByRole("button", { name: "365 days" }))

    expect(within(panel).getAllByText("Base date 2025-10-01")).toHaveLength(5)
    expect(
      within(panel).getByRole("button", { name: "365 days" })
    ).toHaveAttribute("aria-pressed", "true")
  })
  it("shows no per-panel timestamps, methodology or chart-data toggles", () => {
    show(<MacroDashboard data={data} locale="en" />)
    expect(screen.queryByText(/^Updated /)).toBeNull()
    expect(screen.queryByText(/as of/)).toBeNull()
    expect(screen.queryByText("Methodology")).toBeNull()
    expect(screen.queryByText("View chart data")).toBeNull()
    expect(document.querySelector("details")).toBeNull()
  })
  it("omits the introduction, change legend and period note", () => {
    show(<MacroDashboard data={data} locale="en" />)
    expect(screen.queryByText("▲ Up")).toBeNull()
    expect(
      screen.queryByText("Macro, bonds and global currencies in one view.")
    ).toBeNull()
    expect(screen.queryByText(/previous trading day/)).toBeNull()
  })
  it("omits today's overview and economic calendar", () => {
    show(<MacroDashboard data={data} locale="en" />)
    expect(screen.queryByRole("heading", { name: "Today" })).toBeNull()
    expect(
      screen.queryByRole("heading", { name: /Today’s economic calendar/ })
    ).toBeNull()
    for (const name of ["Commodities", "Rates", "Foreign exchange"]) {
      expect(screen.queryByRole("heading", { name })).toBeNull()
    }
    expect(
      screen.getByRole("heading", { name: "Energy / Precious metals" })
    ).toBeVisible()
  })
})
