import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import type { AnchorHTMLAttributes, ReactNode } from "react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import { DailyNews, DailyNewsLoading } from "./DailyNews"
import type {
  Locale,
  NewsroomEdition,
  NewsroomItem,
} from "@daily-insights/api-client"

type MockLinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & {
  children: ReactNode
  params?: { locale?: string; marketCode?: string }
  to: string
}

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, params, to, ...props }: MockLinkProps) => (
    <a
      href={to
        .replace("{-$locale}", params?.locale ?? "")
        .replace("$marketCode", params?.marketCode ?? "")}
      {...props}
    >
      {children}
    </a>
  ),
}))

afterEach(cleanup)

function story(index: number, overrides: Partial<NewsroomItem> = {}) {
  return {
    id: `00000000-0000-4000-8000-0000000001${index.toString().padStart(2, "0")}`,
    event_id: `00000000-0000-4000-8000-0000000002${index.toString().padStart(2, "0")}`,
    rank: index,
    stars: 4,
    headline: `Story ${index}`,
    summary: `Summary ${index}.`,
    why: `Why ${index} matters.`,
    related_symbols: [],
    sources: [],
    ...overrides,
  } satisfies NewsroomItem
}

function edition(
  items: NewsroomItem[],
  overrides: Partial<NewsroomEdition> = {}
): NewsroomEdition {
  return {
    market_code: "global",
    locale: "en",
    edition_id: "00000000-0000-4000-8000-000000000004",
    edition_date: "2026-10-01",
    is_today: true,
    published_at: "2026-10-01T01:00:00Z",
    items,
    ...overrides,
  }
}

function renderNews(
  latest: NewsroomEdition | null,
  { locale = "en", titleKey }: { locale?: Locale; titleKey?: string } = {}
) {
  const i18n = createI18n(locale)
  const view = (next: NewsroomEdition | null, key = titleKey) => (
    <I18nextProvider i18n={i18n}>
      <DailyNews edition={next} {...(key ? { titleKey: key } : {})} />
    </I18nextProvider>
  )
  const result = render(view(latest))
  return {
    ...result,
    panel: within(result.container),
    rerenderNews: (next: NewsroomEdition | null, key?: string) =>
      result.rerender(view(next, key ?? titleKey)),
  }
}

describe("DailyNews", () => {
  it("shows an accessible initial skeleton", () => {
    render(
      <I18nextProvider i18n={createI18n("en")}>
        <DailyNewsLoading />
      </I18nextProvider>
    )
    const skeleton = screen.getByRole("status", { name: "Loading key news" })
    expect(skeleton).toHaveAttribute("aria-live", "polite")
    expect(skeleton).toHaveClass("mb-6")
  })

  it("shows the headline, facts, why it matters and the importance", () => {
    const { panel } = renderNews(edition([story(1), story(2, { stars: null })]))
    const [first, second] = panel.getAllByRole("article")
    const card = within(first!)
    expect(card.getByRole("heading", { name: "Story 1" })).toBeInTheDocument()
    expect(card.getByText("Summary 1.")).toBeInTheDocument()
    expect(
      card.getByRole("heading", { name: "Why it matters" })
    ).toBeInTheDocument()
    expect(card.getByText("Why 1 matters.")).toBeInTheDocument()
    expect(card.getByLabelText("Importance 4 stars")).toHaveTextContent("★★★★")
    expect(
      within(second!).queryByLabelText(/Importance/)
    ).not.toBeInTheDocument()
    // Today's edition carries no date notice.
    expect(panel.queryByText(/being prepared/)).not.toBeInTheDocument()
  })

  it.each([
    ["en", "Today’s news is being prepared. Showing September 30."],
    ["zh-hant", "今日新聞準備中，以下為 9月30日 內容"],
    ["zh-hans", "今日新闻准备中，以下为 9月30日 内容"],
  ] as const)(
    "labels an earlier edition with its date in %s",
    (locale, notice) => {
      const { panel } = renderNews(
        edition([story(1)], {
          locale,
          edition_date: "2026-09-30",
          is_today: false,
        }),
        { locale }
      )
      const heading = panel.getByRole("heading", { level: 2 })
      expect(panel.getByText(notice).parentElement).toBe(heading.parentElement)
    }
  )

  it("links related symbols to the dashboard they appear on", () => {
    const { panel } = renderNews(
      edition([
        story(1, {
          related_symbols: [
            {
              symbol: "^TWII",
              kind: "index",
              label: "TAIEX",
              market_code: "tw_equity",
            },
            {
              symbol: "2330.TW",
              kind: "equity",
              label: "TSMC",
              market_code: null,
            },
          ],
        }),
      ])
    )
    const symbols = panel.getByRole("list", { name: "Related markets" })
    expect(
      within(symbols).getByRole("link", { name: "TAIEX" })
    ).toHaveAttribute("href", "/en/reports/tw_equity")
    expect(
      within(symbols).queryByRole("link", { name: "TSMC" })
    ).not.toBeInTheDocument()
    expect(within(symbols).getByText("TSMC")).toBeInTheDocument()
  })

  it("keeps the sources collapsed until the reader expands them", () => {
    const { panel } = renderNews(
      edition([
        story(1, {
          sources: [
            {
              name: "Reuters",
              url: "https://www.reuters.com/a",
              published_at: "2026-09-30T23:30:00Z",
            },
            {
              name: "Nikkei",
              url: "https://asia.nikkei.com/b",
              published_at: null,
            },
          ],
        }),
        story(2),
      ])
    )
    const toggle = panel.getByText("2 sources")
    const details = toggle.closest("details")!
    expect(details).not.toHaveAttribute("open")
    fireEvent.click(toggle)
    expect(details).toHaveAttribute("open")
    const link = within(details).getByRole("link", { name: "Reuters" })
    expect(link).toHaveAttribute("href", "https://www.reuters.com/a")
    expect(link).toHaveAttribute("target", "_blank")
    expect(link).toHaveAttribute("rel", "noopener noreferrer")
    expect(within(details).getByText("2026-10-01 07:30")).toBeInTheDocument()
    expect(within(details).getAllByRole("listitem")).toHaveLength(2)
    // A story without sources has no empty toggle.
    expect(panel.getAllByRole("group")).toHaveLength(1)
  })

  it("keeps the current cards and page on refresh failure", async () => {
    const latest = edition(
      Array.from({ length: 7 }, (_, index) => story(index + 1)),
      { market_code: "us_equity" }
    )
    const { container, panel, rerenderNews } = renderNews(latest, {
      titleKey: "newsroomTitle_us_equity",
    })
    fireEvent.click(panel.getByRole("button", { name: "Next news page" }))
    await waitFor(() =>
      expect(container.querySelectorAll("article")).toHaveLength(1)
    )
    rerenderNews(null)
    expect(panel.getByText("Story 7")).toBeInTheDocument()
    expect(panel.queryByRole("status")).not.toBeInTheDocument()
    // An authoritative empty response (for example all items hidden) wins.
    rerenderNews(edition([], { edition_id: null, edition_date: null }))
    expect(panel.queryByText("Story 7")).not.toBeInTheDocument()
    expect(panel.getByRole("status")).toHaveTextContent(
      "No key news is available yet."
    )
    rerenderNews(latest)
    rerenderNews(null, "newsroomTitle_tw_equity")
    expect(panel.queryByText("Story 1")).not.toBeInTheDocument()
  })

  it("paginates six cards at a time with looping arrows", async () => {
    const { container, panel } = renderNews(
      edition(Array.from({ length: 13 }, (_, index) => story(index + 1)))
    )
    expect(container.querySelectorAll("article")).toHaveLength(6)
    const previous = panel.getByRole("button", { name: "Previous news page" })
    const next = panel.getByRole("button", { name: "Next news page" })
    expect(
      panel.getByRole("navigation", { name: "News pagination" })
    ).toBeInTheDocument()

    fireEvent.click(previous)
    await waitFor(() =>
      expect(container.querySelectorAll("article")).toHaveLength(1)
    )
    expect(panel.getByText("Story 13")).toBeInTheDocument()
    fireEvent.click(next)
    await waitFor(() => expect(panel.getByText("Story 1")).toBeInTheDocument())
    fireEvent.click(next)
    await waitFor(() => expect(panel.getByText("Story 7")).toBeInTheDocument())
    expect(panel.queryByText("Story 1")).not.toBeInTheDocument()
  })

  it("hides pagination when every story fits on one page", () => {
    const { panel } = renderNews(edition([story(1)]))
    expect(
      panel.queryByRole("navigation", { name: "News pagination" })
    ).not.toBeInTheDocument()
  })

  it("explains a failed first request without any legacy status text", () => {
    const { container, panel } = renderNews(null)
    expect(panel.getByRole("status")).toHaveTextContent(
      "Key news is temporarily unavailable. Reports are not affected."
    )
    expect(
      panel.getByRole("heading", { name: "Today’s major news" })
    ).toBeInTheDocument()
    expect(container.querySelector("section")).toHaveClass("mt-7", "mb-6")
  })
})
