import { cleanup, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import type { VixHistory } from "#/lib/indices"
import { VixHistoryChart, VixHistoryLoading } from "./VixHistoryChart"

vi.mock("@tanstack/react-router", () => ({
  ClientOnly: ({ children }: { children: React.ReactNode }) => children,
}))
vi.mock("echarts-for-react", () => ({
  default: ({ option }: { option: unknown }) => (
    <div data-testid="vix-chart">{JSON.stringify(option)}</div>
  ),
}))

const history: VixHistory = {
  symbol: "^VIX",
  start: "2024-09-03",
  end: "2026-09-03",
  bars: [
    {
      symbol: "^VIX",
      market_code: "us_equity",
      trade_date: "2026-09-01",
      open: "17.50",
      high: "18.40",
      low: "17.10",
      close: "18.10",
      volume: 0,
    },
    {
      symbol: "^VIX",
      market_code: "us_equity",
      trade_date: "2026-09-02",
      open: "22.00",
      high: "25.00",
      low: "21.50",
      close: "24.40",
      volume: 0,
    },
    {
      symbol: "^VIX",
      market_code: "us_equity",
      trade_date: "2026-09-03",
      open: "31.00",
      high: "34.00",
      low: "30.50",
      close: "32.70",
      volume: 0,
    },
  ],
}

async function renderLocalized(ui: React.ReactNode, locale = "en") {
  const i18n = createI18n(locale as "en" | "zh-hant" | "zh-hans")
  await i18n.changeLanguage(locale)
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>)
}

afterEach(cleanup)

describe("VixHistoryChart", () => {
  it("renders two-year closes with the 20 and 30 reference bands", async () => {
    document.documentElement.style.setProperty("--lagoon", "#1eb89b")
    document.documentElement.style.setProperty("--market-caution", "#c98a2b")
    document.documentElement.style.setProperty("--destructive", "#d6453f")

    await renderLocalized(<VixHistoryChart history={history} locale="en" />)

    const chart = screen.getByTestId("vix-chart")
    expect(chart).toHaveTextContent("18.1")
    expect(chart).toHaveTextContent("24.4")
    expect(chart).toHaveTextContent("32.7")
    expect(chart).toHaveTextContent('"yAxis":20')
    expect(chart).toHaveTextContent('"yAxis":30')
    expect(chart).toHaveTextContent("Calm (below 20)")
    expect(chart).toHaveTextContent("Elevated (20–30)")
    expect(chart).toHaveTextContent("High volatility (30 or above)")
    expect(chart).toHaveTextContent("#1eb89b")
    expect(chart).toHaveTextContent("#c98a2b")
    expect(chart).toHaveTextContent("#d6453f")
    expect(screen.getByText(/not official classifications/)).toBeVisible()
    expect(
      screen.getByText("Latest VIX:").nextElementSibling
    ).toHaveTextContent("32.70")
    expect(
      screen.getByRole("caption", {
        name: "VIX daily closing values and reference risk bands",
      })
    ).toBeInTheDocument()
  })

  it("has accessible loading, unavailable, and empty states", async () => {
    await renderLocalized(<VixHistoryLoading />)
    expect(screen.getByRole("status")).toHaveAccessibleName()

    cleanup()
    await renderLocalized(<VixHistoryChart history={null} locale="en" />)
    expect(screen.getByRole("status")).toHaveTextContent("unavailable")

    cleanup()
    await renderLocalized(
      <VixHistoryChart history={{ ...history, bars: [] }} locale="en" />
    )
    expect(screen.getByRole("status")).toHaveTextContent("no VIX daily bars")
  })

  it.each([
    ["zh-hant", "VIX 波動率走勢", "非官方分級"],
    ["zh-hans", "VIX 波动率走势", "并非官方分级"],
    ["en", "VIX volatility trend", "not official classifications"],
  ] as const)("renders %s copy", async (locale, title, reference) => {
    await renderLocalized(
      <VixHistoryChart history={history} locale={locale} />,
      locale
    )

    expect(screen.getByRole("heading", { name: title })).toBeVisible()
    expect(screen.getByText(new RegExp(reference))).toBeVisible()
  })
})
