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
      trade_value: null,
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
      trade_value: null,
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
      trade_value: null,
    },
  ],
  indicators: {
    symbol: "^VIX",
    market_code: "us_equity",
    method: "sma",
    price_field: "close",
    formula_version: "sma-close-v1",
    as_of: "2026-09-03",
    series: [
      { period: 20, points: [] },
      { period: 60, points: [] },
      { period: 120, points: [] },
      { period: 240, points: [] },
    ],
    rsi: {
      period: 14,
      method: "wilder",
      formula_version: "rsi-wilder-close-v1",
      points: [],
    },
    macd: {
      fast_period: 12,
      slow_period: 26,
      signal_period: 9,
      method: "ema",
      formula_version: "macd-ema-close-v1",
      points: [
        {
          trade_date: "2026-09-01",
          macd: "1.1",
          signal: "0.9",
          histogram: "0.2",
        },
        {
          trade_date: "2026-09-02",
          macd: "1.4",
          signal: "1.1",
          histogram: "0.3",
        },
        {
          trade_date: "2026-09-03",
          macd: "1.8",
          signal: "1.4",
          histogram: "0.4",
        },
      ],
    },
    kd: {
      lookback_period: 9,
      k_smoothing_period: 3,
      d_smoothing_period: 3,
      method: "smoothed-rsv",
      formula_version: "stochastic-kd-9-3-3-v1",
      points: [
        { trade_date: "2026-09-01", k: "45", d: "40" },
        { trade_date: "2026-09-02", k: "68", d: "52" },
        { trade_date: "2026-09-03", k: "82", d: "67" },
      ],
    },
  },
}

async function renderLocalized(ui: React.ReactNode, locale = "en") {
  const i18n = createI18n(locale as "en" | "zh-hant" | "zh-hans")
  await i18n.changeLanguage(locale)
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>)
}

afterEach(cleanup)

describe("VixHistoryChart", () => {
  it("renders closes, MACD and KD with one shared zoom", async () => {
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
    expect(chart).toHaveTextContent('"text":"MACD"')
    expect(chart).toHaveTextContent('"text":"KD 9-3-3"')
    expect(chart).toHaveTextContent('"name":"Histogram","type":"bar"')
    expect(chart).toHaveTextContent('"name":"K line","type":"line"')
    expect(chart).toHaveTextContent('"name":"D line","type":"line"')
    expect(chart).not.toHaveTextContent("Volume")
    expect(chart).toHaveTextContent('"xAxisIndex":[0,1,2]')
    expect(chart).toHaveTextContent('"coord":[[0,0],[6,7]],"mergeCells":true')
    expect(chart).toHaveTextContent("#1eb89b")
    expect(chart).toHaveTextContent("#c98a2b")
    expect(chart).toHaveTextContent("#d6453f")
    expect(screen.getByText(/not official classifications/)).toBeVisible()
    expect(
      screen.getByText("Latest VIX:").nextElementSibling
    ).toHaveTextContent("32.70")
    expect(
      screen.getByRole("caption", {
        name: "VIX daily closing values, MACD, KD, and reference risk bands",
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
