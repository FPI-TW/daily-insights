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
import type { NewsroomArticle } from "#/lib/newsroom-admin"
import { EventArticlesPanel } from "./EventArticlesPanel"
import { DATE, event } from "./test-fixtures"

const client = vi.hoisted(() => ({
  eventDetail: vi.fn(),
  splitEvent: vi.fn(),
  mergeEvents: vi.fn(),
  setManualBody: vi.fn(),
}))

vi.mock("#/lib/newsroom-admin", async importOriginal => ({
  ...(await importOriginal<typeof import("#/lib/newsroom-admin")>()),
  browserNewsroomAdminClient: () => client,
}))
vi.mock("#/lib/auth", () => ({
  requireCsrfToken: vi.fn().mockResolvedValue("csrf"),
}))
vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => vi.fn().mockResolvedValue(false),
}))

const subject = event()

function article(overrides: Partial<NewsroomArticle>): NewsroomArticle {
  return {
    id: "a1000000-0000-4000-8000-000000000001",
    source_id: "50000000-0000-4000-8000-0000000000a1",
    source_key: "reuters",
    source_name: "Reuters",
    hostname: "reuters.example",
    title: "Fed holds rates",
    url: "https://reuters.example/fed",
    feed_summary: "The Fed held rates.",
    published_at: null,
    first_seen_at: "2026-09-30T23:00:00+00:00",
    language: "en",
    body_status: "ok",
    body_source: "fetch",
    body_quality_reason: null,
    body_fetched_at: null,
    body_length: 1200,
    body_preview: "The Federal Reserve held its policy rate…",
    fetch_status: "done",
    fetch_error_code: null,
    embed_status: "done",
    triage_status: "done",
    triage_error_code: null,
    relevant: true,
    topic: "policy",
    market_scores: { global: 80 },
    ...overrides,
  }
}

const withBody = article({})
const withoutBody = article({
  id: "a1000000-0000-4000-8000-000000000002",
  source_name: "Blog",
  title: "Fed: what it means",
  body_status: "unavailable",
  body_preview: null,
  body_length: 0,
  triage_status: "pending",
})
const other = event({
  id: "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e19",
  headline_zh_hant: "另一則",
})

function renderPanel() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <I18nextProvider i18n={createI18n("en")}>
        <EventArticlesPanel
          locale="en"
          date={DATE}
          market="global"
          event={subject}
          mergeOptions={[other]}
        />
      </I18nextProvider>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  client.eventDetail.mockResolvedValue({
    event: subject,
    articles: [withBody, withoutBody],
    placements: [
      {
        item_id: "1b000000-0000-4000-8000-000000000001",
        edition_id: "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09",
        market_code: "global",
        edition_status: "draft",
        removed: false,
        hidden: false,
      },
    ],
  })
  client.splitEvent.mockResolvedValue({ event_id: "new-event" })
  client.mergeEvents.mockResolvedValue(undefined)
  client.setManualBody.mockResolvedValue(undefined)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("EventArticlesPanel", () => {
  it("loads the original articles behind a status skeleton", async () => {
    renderPanel()
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading source articles."
    )
    expect(await screen.findByText("Fed holds rates")).toBeVisible()
    expect(screen.getByText("Full text unavailable")).toBeVisible()
    expect(screen.getByText("Triaging")).toBeVisible()
    expect(
      screen.getByText("Full text preview (1200 characters)")
    ).toBeVisible()
  })

  it("splits only a strict subset of the articles", async () => {
    renderPanel()
    const split = await screen.findByRole("button", {
      name: "Split selected articles (0)",
    })
    expect(split).toBeDisabled()
    fireEvent.click(screen.getByLabelText("Select “Fed holds rates”"))
    fireEvent.click(screen.getByLabelText("Select “Fed: what it means”"))
    expect(
      screen.getByRole("button", { name: "Split selected articles (2)" })
    ).toBeDisabled()
    fireEvent.click(screen.getByLabelText("Select “Fed holds rates”"))
    fireEvent.click(
      screen.getByRole("button", { name: "Split selected articles (1)" })
    )
    const confirm = screen.getByRole("alertdialog")
    expect(confirm).toHaveTextContent(/replace any text an editor has changed/)
    expect(client.splitEvent).not.toHaveBeenCalled()
    fireEvent.click(within(confirm).getByRole("button", { name: "Split" }))
    await waitFor(() =>
      expect(client.splitEvent).toHaveBeenCalledWith(
        subject.id,
        [withoutBody.id],
        "csrf"
      )
    )
  })

  it("pastes a body for an article without full text", async () => {
    renderPanel()
    fireEvent.click(
      await screen.findByRole("button", { name: "Paste full text" })
    )
    const dialog = screen.getByRole("dialog")
    fireEvent.change(within(dialog).getByRole("textbox"), {
      target: { value: "第一段。" },
    })
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }))
    await waitFor(() =>
      expect(client.setManualBody).toHaveBeenCalledWith(
        withoutBody.id,
        "第一段。",
        "csrf"
      )
    )
  })

  it("merges chosen events into this one", async () => {
    renderPanel()
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Merge other events into this one",
      })
    )
    const dialog = screen.getByRole("dialog")
    expect(within(dialog).getByRole("note")).toHaveTextContent(
      /replace any text an editor has changed/
    )
    fireEvent.click(within(dialog).getByLabelText("另一則"))
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Merge 1 event" })
    )
    await waitFor(() =>
      expect(client.mergeEvents).toHaveBeenCalledWith(
        subject.id,
        [other.id],
        "csrf"
      )
    )
  })
})
