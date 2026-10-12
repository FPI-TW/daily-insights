import { cleanup, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it } from "vitest"
import type { IndexLatestBar } from "@daily-insights/api-client"
import { createI18n } from "#/lib/i18n"
import { HkIndexCards, HkIndexCardsLoading } from "./HkIndexCards"

function bar(
  symbol: string,
  close: string,
  previous_close: string | null
): IndexLatestBar {
  return {
    symbol,
    market_code: "hk_equity",
    trade_date: "2026-10-09",
    open: null,
    high: null,
    low: null,
    close,
    volume: null,
    trade_value: null,
    previous_close,
  }
}

async function renderLocalized(
  ui: React.ReactNode,
  locale: "zh-hant" | "zh-hans" | "en" = "zh-hant"
) {
  const i18n = createI18n(locale)
  await i18n.changeLanguage(locale)
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>)
}
afterEach(cleanup)

describe("HkIndexCards", () => {
  it("renders every catalog index with close, point and percent change", async () => {
    await renderLocalized(
      <HkIndexCards
        locale="zh-hant"
        bars={[
          bar("^HSI", "24211.35", "23785.79"),
          bar("^HSCE", "8147.87", "8200.00"),
          bar("^HSIL", "18.09", "18.09"),
        ]}
      />
    )
    const hsi = screen.getByRole("region", { name: "恆生指數" })
    expect(hsi).toHaveTextContent("恆生指數 · HSI")
    expect(hsi).toHaveTextContent("24,211.35")
    expect(hsi).toHaveTextContent("+425.56")
    expect(hsi).toHaveTextContent("+1.79%")
    expect(hsi).toHaveClass("border-l-market-up")

    const hsce = screen.getByRole("region", { name: "國企指數" })
    expect(hsce).toHaveTextContent("HSCEI")
    expect(hsce).toHaveTextContent("-52.13")
    expect(hsce).toHaveTextContent("-0.64%")
    expect(hsce).toHaveClass("border-l-market-down")

    const vix = screen.getByRole("region", { name: "港股 VIX" })
    expect(vix).toHaveTextContent("HSI VIX")
    expect(vix).toHaveTextContent("持平")
    expect(vix).toHaveClass("border-l-line")
  })

  it("keeps a placeholder card for symbols without rows or a previous close", async () => {
    await renderLocalized(
      <HkIndexCards locale="en" bars={[bar("^HSI", "24211.35", null)]} />,
      "en"
    )
    const hsi = screen.getByRole("region", { name: "Hang Seng Index" })
    expect(hsi).toHaveTextContent("24,211.35")
    expect(hsi).toHaveTextContent("—")
    expect(hsi).toHaveClass("border-l-line")
    const hsce = screen.getByRole("region", {
      name: "Hang Seng China Enterprises Index",
    })
    expect(hsce).toHaveTextContent("HSCEI")
    expect(hsce).toHaveTextContent("—")
    expect(screen.getAllByRole("region")).toHaveLength(3)
  })

  it("announces loading and unavailable states", async () => {
    await renderLocalized(<HkIndexCardsLoading />, "en")
    expect(screen.getByRole("status")).toHaveAccessibleName(
      "Loading Hong Kong indices."
    )
    cleanup()
    await renderLocalized(<HkIndexCards locale="en" bars={null} />, "en")
    expect(screen.getByRole("status")).toHaveTextContent("unavailable")
  })
})
