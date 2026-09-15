import { cleanup, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, expect, it } from "vitest"
import { createI18n } from "#/lib/i18n"
import { TaiwanSourceDiagnostics } from "./TaiwanSourceDiagnostics"

afterEach(cleanup)

it("isolates failures by endpoint and does not count unpublished dates as failures", () => {
  render(
    <I18nextProvider i18n={createI18n("zh-hant")}>
      <TaiwanSourceDiagnostics
        error="twse_fetch_failures"
        result={{
          market_flows: {
            covered_trading_days: 40,
            lookback_trading_days: 40,
            aborted: false,
            days: [
              { trade_date: "2026-09-14", status: "no_data" },
              { trade_date: "2026-09-11", status: "existing" },
            ],
          },
          stock_flows: {
            covered_trading_days: 0,
            lookback_trading_days: 1,
            aborted: true,
            days: [
              { trade_date: "2026-09-11", status: "failed", error: "timeout" },
            ],
          },
        }}
      />
    </I18nextProvider>
  )
  expect(
    screen.getByText("TWSE · BFI82U · 三大法人每日買賣超 · 正常")
  ).toBeInTheDocument()
  expect(
    screen.getByText("TWSE · T86 · 三大法人買賣超個股 · 無法使用")
  ).toBeInTheDocument()
  expect(screen.getByText("沿用既有資料")).toBeInTheDocument()
  expect(screen.getByText("timeout")).toBeInTheDocument()
})

it("keeps missing historical details unknown", () => {
  render(
    <I18nextProvider i18n={createI18n("en")}>
      <TaiwanSourceDiagnostics result={null} error={null} />
    </I18nextProvider>
  )
  expect(screen.getAllByText(/Source details were not recorded/)).toHaveLength(
    3
  )
})
