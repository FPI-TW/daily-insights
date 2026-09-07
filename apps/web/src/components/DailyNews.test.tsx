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
            caveat: "Showing the latest available news from 2026-09-01.",
            items: [
              {
                id: "00000000-0000-4000-8000-000000000001",
                rank: 1,
                importance: 4,
                topic: "markets",
                headline: "Markets move",
                summary: "A grounded summary.",
                source_name: "Reuters",
                source_hostname: "www.reuters.com",
                source_url: "https://www.reuters.com/example",
                source_published_at: "2026-09-01T00:00:00+00:00",
                numeric_facts: [],
                market: null,
                event_key: null,
              },
            ],
          }}
        />
      </I18nextProvider>
    )
    expect(
      screen.getByText("Showing the latest available news from 2026-09-01.")
    ).toBeInTheDocument()
    const link = screen.getByRole("link", { name: "Read source" })
    expect(link).toHaveAttribute("rel", "noopener noreferrer")
    expect(screen.getByLabelText("Importance 4 stars")).toHaveTextContent(
      "★★★★"
    )
  })

  it("shows a partial edition as a plain list and hides missing publish times", async () => {
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
                source_hostname: "apnews.com",
                source_url: "https://apnews.com/example",
                source_published_at: null,
                numeric_facts: [],
                market: null,
                event_key: null,
              },
            ],
          }}
        />
      </I18nextProvider>
    )
    const panel = within(container)
    expect(panel.queryByText("Partial")).not.toBeInTheDocument()
    expect(panel.queryByText(/of 5 stories/)).not.toBeInTheDocument()
    expect(panel.queryByText(/Time unavailable/)).not.toBeInTheDocument()
    expect(container.querySelector("time")).toBeNull()
  })

  it("groups stories by market and keeps numeric facts out of the card", async () => {
    const i18n = createI18n("zh-hant")
    await i18n.changeLanguage("zh-hant")
    const item = (index: number, market: "us" | "taiwan", facts: string[]) => ({
      id: `00000000-0000-4000-8000-00000000001${index}`,
      rank: index,
      importance: 3,
      topic: "markets" as const,
      headline: `Story ${index}`,
      summary: "Summary.",
      source_name: "Source",
      source_hostname: "source.example",
      source_url: `https://source.example/${index}`,
      source_published_at: null,
      numeric_facts: facts,
      market,
      event_key: `event-${index}`,
    })
    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <DailyNews
          news={{
            market_code: "global",
            target_items: 5,
            edition_id: "00000000-0000-4000-8000-000000000002",
            edition_date: "2026-09-03",
            revision: 1,
            status: "complete",
            locale: "zh-hant",
            generated_at: "2026-09-03T00:00:00+00:00",
            caveat: null,
            items: [
              item(1, "us", ["+3.2%", "1 碼"]),
              item(2, "taiwan", []),
              item(3, "us", ["-0.5%"]),
            ],
          }}
        />
      </I18nextProvider>
    )
    const panel = within(container)
    const groups = panel.getAllByRole("heading", { level: 3 })
    expect(groups.map(heading => heading.textContent)).toEqual([
      "美國",
      "Story 1",
      "Story 3",
      "台灣",
      "Story 2",
    ])
    // numeric_facts is the summary's grounding record, not reader content:
    // stripped of their sentences the figures are ambiguous, so cards omit them.
    expect(panel.queryByText("+3.2%")).not.toBeInTheDocument()
    expect(panel.queryByText("1 碼")).not.toBeInTheDocument()
  })

  it("renders a market page's stories without market headings", async () => {
    const i18n = createI18n("zh-hant")
    await i18n.changeLanguage("zh-hant")
    const item = (index: number, market: "taiwan" | "global") => ({
      id: `00000000-0000-4000-8000-00000000002${index}`,
      rank: index,
      importance: 3,
      topic: "markets" as const,
      headline: `Story ${index}`,
      summary: "Summary.",
      source_name: "Source",
      source_hostname: "source.example",
      source_url: `https://source.example/${index}`,
      source_published_at: null,
      numeric_facts: [],
      market,
      event_key: `event-${index}`,
    })
    const { container } = render(
      <I18nextProvider i18n={i18n}>
        <DailyNews
          news={{
            market_code: "tw_equity",
            target_items: 8,
            edition_id: "00000000-0000-4000-8000-000000000003",
            edition_date: "2026-09-04",
            revision: 1,
            status: "partial",
            locale: "zh-hant",
            generated_at: "2026-09-04T00:00:00+00:00",
            caveat: null,
            items: [item(1, "taiwan"), item(2, "global")],
          }}
          eyebrowKey="marketNewsEyebrow"
          titleKey="marketNewsTitle_tw_equity"
          groupByMarket={false}
        />
      </I18nextProvider>
    )
    const panel = within(container)
    expect(
      panel.getAllByRole("heading", { level: 3 }).map(h => h.textContent)
    ).toEqual(["Story 1", "Story 2"])
    expect(panel.queryByText("全球")).not.toBeInTheDocument()
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
