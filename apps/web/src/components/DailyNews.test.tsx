import { render, screen, within } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { describe, expect, it } from "vitest"
import { createI18n } from "#/lib/i18n"
import { DailyNews, DailyNewsLoading } from "./DailyNews"

describe("DailyNews", () => {
  it("shows an accessible initial skeleton", async () => {
    const i18n = createI18n("en")
    await i18n.changeLanguage("en")
    render(
      <I18nextProvider i18n={i18n}>
        <DailyNewsLoading />
      </I18nextProvider>
    )
    expect(screen.getByRole("status")).toBeInTheDocument()
  })

  it("shows source metadata, importance, and a hardened external link", async () => {
    const i18n = createI18n("en")
    await i18n.changeLanguage("en")
    render(
      <I18nextProvider i18n={i18n}>
        <DailyNews
          news={{
            market_code: "global",
            target_items: 5,
            edition_id: "00000000-0000-4000-8000-000000000002",
            edition_date: "2026-09-01",
            revision: 1,
            status: "complete",
            locale: "en",
            generated_at: "2026-09-01T00:00:00+00:00",
            caveat: null,
            items: [
              {
                id: "00000000-0000-4000-8000-000000000001",
                rank: 1,
                importance: 4,
                topic: "markets",
                headline: "Markets move",
                summary: "A grounded summary.",
                source_name: "Reuters",
                source_url: "https://www.reuters.com/example",
                source_published_at: "2026-09-01T00:00:00+00:00",
              },
            ],
          }}
        />
      </I18nextProvider>
    )
    const link = screen.getByRole("link", { name: "Read source" })
    expect(link).toHaveAttribute("rel", "noopener noreferrer")
    expect(screen.getByLabelText("Importance 4 stars")).toHaveTextContent(
      "★★★★"
    )
  })

  it("explains a partial edition and hides missing publish times", async () => {
    const i18n = createI18n("en")
    await i18n.changeLanguage("en")
    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <DailyNews
          news={{
            market_code: "global",
            target_items: 5,
            edition_id: "00000000-0000-4000-8000-000000000002",
            edition_date: "2026-09-01",
            revision: 1,
            status: "partial",
            locale: "en",
            generated_at: "2026-09-01T00:00:00+00:00",
            caveat: null,
            items: [
              {
                id: "00000000-0000-4000-8000-000000000001",
                rank: 1,
                importance: 3,
                topic: "markets",
                headline: "Undated story",
                summary: "A grounded summary.",
                source_name: "AP",
                source_url: "https://apnews.com/example",
                source_published_at: null,
              },
            ],
          }}
        />
      </I18nextProvider>
    )
    const panel = within(container)
    expect(panel.getByText("Partial")).toBeInTheDocument()
    expect(
      panel.getByText("Only 1 of 5 stories were produced today.")
    ).toBeInTheDocument()
    expect(panel.queryByText(/Time unavailable/)).not.toBeInTheDocument()
    expect(container.querySelector("time")).toBeNull()
  })

  it("degrades to an unavailable panel when the news request failed", async () => {
    const i18n = createI18n("en")
    await i18n.changeLanguage("en")
    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <DailyNews news={null} />
      </I18nextProvider>
    )
    const panel = within(container)
    expect(
      panel.getByText(/Today’s major news could not be loaded right now/)
    ).toHaveAttribute("role", "status")
    expect(panel.getByText("Unavailable")).toBeInTheDocument()
    expect(panel.queryByRole("link")).not.toBeInTheDocument()
  })
})
