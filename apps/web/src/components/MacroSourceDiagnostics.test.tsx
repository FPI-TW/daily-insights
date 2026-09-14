import { cleanup, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, expect, it } from "vitest"
import { createI18n } from "#/lib/i18n"
import { MacroSourceDiagnostics } from "./MacroSourceDiagnostics"

afterEach(cleanup)

it("shows multiple failures and disabled Nasdaq with localized descriptions", () => {
  render(
    <I18nextProvider i18n={createI18n("zh-hant")}>
      <MacroSourceDiagnostics
        error="macro_sources_unavailable"
        result={{
          sources: [
            {
              code: "yahoo_finance",
              name: "Yahoo Finance",
              status: "degraded",
              fetched_at: "2026-09-14T00:00:00Z",
              affected_items: ["DX-Y.NYB"],
              failures: [
                {
                  endpoint: "history",
                  affected_items: ["DX-Y.NYB"],
                  failure_type: "timeout",
                  http_status: null,
                },
              ],
            },
            {
              code: "new_york_fed",
              name: "New York Fed",
              status: "unavailable",
              fetched_at: "2026-09-14T00:00:00Z",
              affected_items: ["SOFR"],
              failures: [
                {
                  endpoint: "sofr/search.json",
                  affected_items: ["SOFR"],
                  failure_type: "http_error",
                  http_status: 503,
                },
              ],
            },
            {
              code: "nasdaq_calendar",
              name: "Nasdaq",
              status: "disabled",
              fetched_at: "2026-09-14T00:00:00Z",
              affected_items: [],
              failures: [],
            },
          ],
        }}
      />
    </I18nextProvider>
  )
  expect(screen.getByText("Yahoo Finance · 部分失敗")).toBeInTheDocument()
  expect(screen.getByText(/history · 請求逾時/)).toBeInTheDocument()
  expect(screen.getByText(/HTTP 503/)).toBeInTheDocument()
  expect(screen.getByText("Nasdaq · 已停用")).toBeInTheDocument()
})

it("keeps historical errors without inventing source details", () => {
  render(
    <I18nextProvider i18n={createI18n("en")}>
      <MacroSourceDiagnostics result={{}} error="macro_calendar_unavailable" />
    </I18nextProvider>
  )
  expect(
    screen.getByText("Source details were not recorded")
  ).toBeInTheDocument()
  expect(screen.getByText(/Economic calendar unavailable/)).toBeInTheDocument()
})
