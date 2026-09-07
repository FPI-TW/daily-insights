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
  calendar: { date: "2026-09-04", status: "disabled", events: [] },
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
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "usd_jpy" },
    })
    expect(
      screen.getByRole("img", { name: /trends · USD\/JPY/ })
    ).toHaveTextContent("145")
    fireEvent.click(screen.getByRole("button", { name: "365 days" }))
    expect(
      screen.getByRole("img", { name: /trends · USD\/JPY/ })
    ).toHaveTextContent("2026-01-02")
    expect(screen.getByRole("button", { name: "365 days" })).toHaveAttribute(
      "aria-pressed",
      "true"
    )
  })
  it("distinguishes a disabled calendar from a successfully loaded empty calendar", () => {
    show(<MacroDashboard data={data} locale="en" />)
    const panel = screen
      .getByRole("heading", { name: /Today’s economic calendar/ })
      .closest("section")!
    expect(within(panel).getByRole("status")).toHaveTextContent(
      "temporarily unavailable"
    )
    expect(screen.queryByText(/no scheduled events/)).toBeNull()
    cleanup()
    show(
      <MacroDashboard
        data={{ ...data, calendar: { ...data.calendar, status: "ok" } }}
        locale="en"
      />
    )
    expect(screen.getByText(/no scheduled events/)).toBeVisible()
  })
  it("shows actual zero as zero and keeps missing actual values distinct", () => {
    show(
      <MacroDashboard
        data={{
          ...data,
          calendar: {
            date: "2026-09-04",
            status: "ok",
            events: [
              {
                date: "2026-09-04T00:00:00Z",
                country: "US",
                event: "Test release",
                currency: "USD",
                impact: "High",
                estimate: "1",
                previous: "2",
                actual: "0",
                unit: "%",
              },
            ],
          },
        }}
        locale="en"
      />
    )
    expect(
      screen.getByText("Test release (%)").closest("tr")
    ).toHaveTextContent("High120")
  })
})
