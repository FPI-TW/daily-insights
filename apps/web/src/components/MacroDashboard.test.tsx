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
    source: "Nasdaq",
    status: "disabled",
    events: [],
  },
  histories: ["eur_usd", "usd_jpy"].map((id, index) => ({
    id,
    symbol: id,
    unit: "USD",
    source: "Yahoo Finance",
    status: "ok",
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
  it("keeps the change legend grouped with the left-side introduction", () => {
    show(<MacroDashboard data={data} locale="en" />)
    const legend = screen.getByText("▲ Up").parentElement!
    const introduction = screen.getByText(
      "Macro, bonds and global currencies in one view."
    )
    expect(legend.parentElement).toBe(introduction.parentElement)
    expect(legend.parentElement).toHaveClass("flex")
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
