import { ApiError } from "@daily-insights/api-client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import type { NewsroomMarket } from "#/lib/newsroom-admin"
import { NewsroomReviewPage } from "./NewsroomReviewPage"
import {
  DATE,
  EDITION_ID,
  day,
  detail,
  edition,
  event,
  item,
} from "./test-fixtures"

const client = vi.hoisted(() => ({
  editionDay: vi.fn(),
  editionDetail: vi.fn(),
  eventDetail: vi.fn(),
  publishEdition: vi.fn(),
  publishDay: vi.fn(),
  addItem: vi.fn(),
  reorder: vi.fn(),
  itemAction: vi.fn(),
  editWhy: vi.fn(),
  editEvent: vi.fn(),
  reanalyze: vi.fn(),
  mergeEvents: vi.fn(),
  splitEvent: vi.fn(),
  setManualBody: vi.fn(),
  submitManualUrl: vi.fn(),
}))
const redirectExpired = vi.hoisted(() => vi.fn().mockResolvedValue(false))

vi.mock("#/lib/newsroom-admin", async importOriginal => ({
  ...(await importOriginal<typeof import("#/lib/newsroom-admin")>()),
  browserNewsroomAdminClient: () => client,
}))
vi.mock("#/lib/auth", () => ({
  requireCsrfToken: vi.fn().mockResolvedValue("csrf"),
}))
vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => redirectExpired,
}))

const first = item()
const second = item({
  id: "1b000000-0000-4000-8000-000000000002",
  rank: 2,
  stars: 4,
  why_zh_hant: null,
  why_status: "pending",
  event: event({
    id: "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e12",
    working_title: "Chip export rules",
    headline_zh_hant: null,
    summary_zh_hant: null,
    analysis_status: "needs_body",
    body_ok_count: 0,
  }),
})
const candidateEvent = event({
  id: "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e13",
  working_title: "Oil supply cut",
  headline_zh_hant: null,
  analysis_status: "idle",
})

function renderPage({
  market = "global",
  onMarketChange = vi.fn(),
}: {
  market?: NewsroomMarket
  onMarketChange?: (market: NewsroomMarket) => void
} = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <I18nextProvider i18n={createI18n("en")}>
        <NewsroomReviewPage
          locale="en"
          date={DATE}
          market={market}
          onDateChange={vi.fn()}
          onMarketChange={onMarketChange}
        />
      </I18nextProvider>
    </QueryClientProvider>
  )
  return { ...view, queryClient }
}

beforeEach(() => {
  client.editionDay.mockResolvedValue(day())
  client.editionDetail.mockResolvedValue(
    detail({
      items: [first, second],
      candidates: [{ score: 72, event: candidateEvent }],
    })
  )
  for (const action of [
    client.publishEdition,
    client.publishDay,
    client.reorder,
    client.itemAction,
    client.editWhy,
    client.editEvent,
    client.reanalyze,
  ]) {
    action.mockResolvedValue(undefined)
  }
  client.addItem.mockResolvedValue({ item_id: "new-item" })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("NewsroomReviewPage", () => {
  it("shows a status skeleton until the first response, never an empty state", async () => {
    let resolve: (value: unknown) => void = () => {}
    client.editionDetail.mockReturnValue(
      new Promise(done => {
        resolve = done
      })
    )
    renderPage()

    const loading = screen.getByRole("status")
    expect(loading).toHaveAttribute("aria-live", "polite")
    expect(loading).toHaveTextContent("Loading key news review.")
    expect(
      screen.queryByText(/This edition has no items/)
    ).not.toBeInTheDocument()

    resolve(detail())
    expect(
      await screen.findByText(/This edition has no items/)
    ).toBeInTheDocument()
    expect(screen.getByText("No other candidate events.")).toBeInTheDocument()
  })

  it("explains a date that has not been assembled", async () => {
    client.editionDay.mockResolvedValue(day(null))
    client.editionDetail.mockResolvedValue(
      detail({
        edition: null,
        candidates: [{ score: 50, event: candidateEvent }],
      })
    )
    renderPage()

    expect(await screen.findByText(/has not been assembled/)).toBeVisible()
    expect(
      screen.getByRole("button", { name: /Add “Oil supply cut”/ })
    ).toBeDisabled()
    expect(
      screen.getByRole("button", { name: /Approve all drafts \(0\)/ })
    ).toBeDisabled()
  })

  it("flags the review states an editor must notice", async () => {
    client.editionDay.mockResolvedValue(
      day(edition({ selection_mode: "fallback", ignored_pending_triage: 2 }))
    )
    client.editionDetail.mockResolvedValue(
      detail({
        edition: edition({
          selection_mode: "fallback",
          ignored_pending_triage: 2,
        }),
        items: [first, second],
      })
    )
    renderPage()

    expect(await screen.findByText(/editor pass failed/)).toBeVisible()
    expect(screen.getByText(/2 untriaged articles were skipped/)).toBeVisible()
    expect(screen.getByText("4 articles not triaged yet")).toBeVisible()
    const card = screen.getByRole("article", { name: "Chip export rules" })
    expect(within(card).getByText("Missing full text")).toBeVisible()
    expect(within(card).getByText("Writing “why it matters”")).toBeVisible()
    expect(
      within(card).getByRole("button", { name: "Edit headline / summary" })
    ).toBeDisabled()
    expect(
      within(screen.getByRole("tab", { name: /Global/ })).getByText("Draft")
    ).toBeVisible()
  })

  it("approves one market after confirmation", async () => {
    renderPage()
    fireEvent.click(
      await screen.findByRole("button", { name: "Approve this market" })
    )
    const dialog = screen.getByRole("alertdialog")
    fireEvent.click(within(dialog).getByRole("button", { name: "Approve" }))

    await waitFor(() =>
      expect(client.publishEdition).toHaveBeenCalledWith(EDITION_ID, "csrf")
    )
    await waitFor(() =>
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
    )
  })

  it("approves every draft of the date at once", async () => {
    renderPage()
    fireEvent.click(
      await screen.findByRole("button", { name: "Approve all drafts (1)" })
    )
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", {
        name: "Approve",
      })
    )
    await waitFor(() =>
      expect(client.publishDay).toHaveBeenCalledWith(DATE, "csrf")
    )
  })

  it("removes a draft item and refetches only this date", async () => {
    const { queryClient } = renderPage()
    const invalidate = vi.spyOn(queryClient, "invalidateQueries")
    const card = await screen.findByRole("article", {
      name: "聯準會維持利率不變",
    })
    fireEvent.click(within(card).getByRole("button", { name: "Remove" }))

    await waitFor(() =>
      expect(client.itemAction).toHaveBeenCalledWith(first.id, "remove", "csrf")
    )
    await waitFor(() => expect(invalidate).toHaveBeenCalledTimes(2))
    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual(
      [
        ["newsroom-admin", "edition", DATE, "global"],
        ["newsroom-admin", "day", DATE],
      ]
    )
  })

  it("hides rather than removes once the edition is published", async () => {
    const published = edition({
      status: "published",
      published_at: "2026-10-01T00:30:00+00:00",
      published_by_user_id: "40000000-0000-4000-8000-000000000001",
    })
    client.editionDay.mockResolvedValue(day(published))
    client.editionDetail.mockResolvedValue(
      detail({ edition: published, items: [first] })
    )
    renderPage()

    const card = await screen.findByRole("article", {
      name: "聯準會維持利率不變",
    })
    expect(
      within(card).queryByRole("button", { name: "Remove" })
    ).not.toBeInTheDocument()
    expect(screen.getByText(/Approved by an editor at 08:30/)).toBeVisible()
    fireEvent.click(within(card).getByRole("button", { name: "Hide" }))
    await waitFor(() =>
      expect(client.itemAction).toHaveBeenCalledWith(first.id, "hide", "csrf")
    )
  })

  it("edits a market's why it matters", async () => {
    renderPage()
    const card = await screen.findByRole("article", {
      name: "聯準會維持利率不變",
    })
    fireEvent.click(
      within(card).getByRole("button", { name: "Edit “why it matters”" })
    )
    const dialog = screen.getByRole("dialog")
    fireEvent.change(within(dialog).getByRole("textbox"), {
      target: { value: "  資金流向改變。 " },
    })
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }))

    await waitFor(() =>
      expect(client.editWhy).toHaveBeenCalledWith(
        first.id,
        "資金流向改變。",
        "csrf"
      )
    )
  })

  it("edits the shared headline and drops a related symbol", async () => {
    renderPage()
    const card = await screen.findByRole("article", {
      name: "聯準會維持利率不變",
    })
    fireEvent.click(
      within(card).getByRole("button", { name: "Edit headline / summary" })
    )
    const dialog = screen.getByRole("dialog")
    fireEvent.change(within(dialog).getByLabelText("Headline"), {
      target: { value: "聯準會按兵不動" },
    })
    fireEvent.click(
      within(dialog).getByRole("button", {
        name: "Remove related symbol S&P 500",
      })
    )
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }))

    await waitFor(() =>
      expect(client.editEvent).toHaveBeenCalledWith(
        first.event.id,
        { headline: "聯準會按兵不動", related_symbols: [] },
        "csrf"
      )
    )
  })

  it("reorders by moving an item down", async () => {
    renderPage()
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Move “聯準會維持利率不變” down",
      })
    )
    await waitFor(() =>
      expect(client.reorder).toHaveBeenCalledWith(
        EDITION_ID,
        [second.id, first.id],
        "csrf"
      )
    )
  })

  it("adds a candidate event and re-analyses an item", async () => {
    renderPage()
    fireEvent.click(
      await screen.findByRole("button", { name: /Add “Oil supply cut”/ })
    )
    await waitFor(() =>
      expect(client.addItem).toHaveBeenCalledWith(
        EDITION_ID,
        candidateEvent.id,
        "csrf"
      )
    )
    const card = screen.getByRole("article", { name: "聯準會維持利率不變" })
    fireEvent.click(within(card).getByRole("button", { name: "Re-analyse" }))
    await waitFor(() =>
      expect(client.reanalyze).toHaveBeenCalledWith(first.event.id, "csrf")
    )
  })

  it("explains a conflict instead of failing silently", async () => {
    client.itemAction.mockRejectedValue(
      new ApiError(409, null, "edition is already published")
    )
    renderPage()
    const card = await screen.findByRole("article", {
      name: "聯準會維持利率不變",
    })
    fireEvent.click(within(card).getByRole("button", { name: "Remove" }))

    expect(await within(card).findByRole("alert")).toHaveTextContent(
      /The state changed/
    )
    expect(redirectExpired).toHaveBeenCalled()
  })

  it("switches market through the tab list", async () => {
    const onMarketChange = vi.fn()
    renderPage({ onMarketChange })
    fireEvent.click(await screen.findByRole("tab", { name: /US equities/ }))
    expect(onMarketChange).toHaveBeenCalledWith("us_equity")
    expect(screen.getByRole("tab", { name: /Global/ })).toHaveAttribute(
      "aria-selected",
      "true"
    )
  })

  it("queues an off-pool URL for the reviewed date", async () => {
    client.submitManualUrl.mockResolvedValue({ article_id: "a" })
    renderPage()
    const input = await screen.findByLabelText("Article URL")
    fireEvent.change(input, { target: { value: "ftp://nope" } })
    fireEvent.click(screen.getByRole("button", { name: "Submit" }))
    expect(await screen.findByText("Enter an http or https URL.")).toBeVisible()
    expect(client.submitManualUrl).not.toHaveBeenCalled()

    fireEvent.change(input, {
      target: { value: "https://news.example/story" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Submit" }))
    await waitFor(() =>
      expect(client.submitManualUrl).toHaveBeenCalledWith(
        "https://news.example/story",
        DATE,
        "csrf"
      )
    )
    expect(
      await screen.findByText("Queued: https://news.example/story")
    ).toBeVisible()
  })
})
