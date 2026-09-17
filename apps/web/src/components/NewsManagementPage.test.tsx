import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { ApiError } from "@daily-insights/api-client"
import {
  cleanup,
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import { createI18n } from "#/lib/i18n"
import { NewsManagementPage } from "./NewsManagementPage"

const {
  catalog,
  listNewsRuns,
  createRun,
  cancelRun,
  listEditions,
  hideItem,
  unhideItem,
  publishCandidates,
  redirectExpired,
  recoveryStatus,
} = vi.hoisted(() => ({
  catalog: vi.fn(),
  listNewsRuns: vi.fn(),
  createRun: vi.fn(),
  cancelRun: vi.fn(),
  listEditions: vi.fn(),
  hideItem: vi.fn(),
  unhideItem: vi.fn(),
  publishCandidates: vi.fn(),
  redirectExpired: vi.fn().mockResolvedValue(false),
  recoveryStatus: vi.fn().mockResolvedValue({ dependencies: [] }),
}))

vi.mock("#/lib/admin-members", () => ({
  browserAdministrationClient: () => ({
    dataManagementCatalog: catalog,
    listJobRuns: listNewsRuns,
    createJobRun: createRun,
    cancelJobRun: cancelRun,
    listNewsEditions: listEditions,
    hideNewsItem: hideItem,
    unhideNewsItem: unhideItem,
    publishNewsCandidates: publishCandidates,
    newsRecoveryStatus: recoveryStatus,
  }),
}))

const enabledCatalog = {
  taipei_date: "2026-09-07",
  daily_news_enabled: true,
  news_markets: ["global", "tw_equity", "us_equity"],
}

function jobRun(overrides: Record<string, unknown> = {}) {
  return {
    id: "c744cb20-bf7c-4f4a-8e7b-1e0c69a91adf",
    routine_run_id: null,
    job_key: "news_daily_update",
    kind: "function",
    trigger: "manual",
    edition_date: enabledCatalog.taipei_date,
    deadline_at: null,
    status: "pending",
    requested_by_user_id: "ee77eab0-3910-4706-803c-ffaf979f1ff7",
    payload: null,
    started_at: null,
    completed_at: null,
    result: null,
    error: null,
    created_at: "2026-09-07T00:00:00+00:00",
    functions: [],
    depends_on: [],
    downstream_jobs: [],
    ...overrides,
  }
}

const editionId = "68f17dd0-06d0-4c95-aa5d-f22ccdc6cf09"
const itemId = "0f9b6a6e-3d7f-4f4f-9a3f-2b7d3f1c9e11"
const droppedId = "2a5e0d3e-5b2c-4d51-8f2e-6f0d3a9c1b22"
const reviewedId = "3b6f1e4f-6c3d-4e62-9a3f-7a1e4b0d2c33"

function candidate(overrides: Record<string, unknown>) {
  return {
    id: droppedId,
    stage: "dropped",
    drop_reason: "off_market",
    headline: "Fed holds rates",
    source_name: "Reuters",
    hostname: "www.reuters.com",
    url: "https://www.reuters.com/fed",
    seen_at: "2026-09-07T00:00:00+00:00",
    source_published_at: null,
    ai_rank: 2,
    ai_topic: "policy",
    ai_market: "us",
    ai_importance: 4,
    ai_event_key: "fed",
    item_id: null,
    publish_run_id: null,
    publish_requested_at: null,
    publish_error: null,
    ...overrides,
  }
}

const globalEdition = {
  market_code: "global",
  edition: {
    id: editionId,
    revision: 2,
    status: "complete",
    generated_at: "2026-09-07T00:05:00+00:00",
    prompt_version: "selection-v7:abc",
    target_items: 5,
    counts: {
      discovered: 4,
      fetch_failed: 1,
      unused: 2,
      reviewed: 3,
      prepared: 0,
      dropped: 1,
      published: 1,
      hidden: 0,
    },
  },
  items: [
    {
      id: itemId,
      rank: 1,
      origin: "model",
      hidden: false,
      hidden_at: null,
      headline: "台積電法說",
      source_headline: "TSMC earnings call",
      source_name: "Reuters",
      source_hostname: "www.reuters.com",
      source_url: "https://www.reuters.com/tsmc",
      source_published_at: null,
      topic: "companies",
      market: "taiwan",
      importance: 4,
      event_key: "tsmc-earnings",
      candidate_id: null,
    },
  ],
  candidates: [
    candidate({}),
    candidate({
      id: reviewedId,
      stage: "reviewed",
      drop_reason: null,
      headline: "Oil slips",
      url: "https://www.reuters.com/oil",
      ai_rank: null,
      ai_topic: null,
      ai_market: null,
      ai_importance: null,
      ai_event_key: null,
    }),
    candidate({
      id: "4c7a2f5a-7d4e-4f73-8b4a-8b2f5c1e3d44",
      stage: "published",
      drop_reason: null,
      headline: "TSMC earnings call",
      url: "https://www.reuters.com/tsmc",
      ai_rank: 1,
      item_id: itemId,
    }),
  ],
}

const emptyMarket = (market_code: string) => ({
  market_code,
  edition: null,
  items: [],
  candidates: [],
})

const editions = {
  edition_date: "2026-09-07",
  editions: [globalEdition, emptyMarket("tw_equity"), emptyMarket("us_equity")],
}
vi.mock("#/lib/auth", () => ({
  requireCsrfToken: vi.fn().mockResolvedValue("csrf"),
}))
vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => redirectExpired,
}))

function renderPage(
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
) {
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={createI18n("en")}>
        <NewsManagementPage locale="en" />
      </I18nextProvider>
    </QueryClientProvider>
  )
}

// The rerun buttons and the curation market tabs share their labels, so the
// rerun assertions are scoped to their own region.
function marketRerunRegion() {
  return screen.getByRole("region", { name: "Single-market news refresh" })
}

afterEach(() => {
  cleanup()
  catalog.mockReset()
  listNewsRuns.mockReset()
  createRun.mockReset()
  cancelRun.mockReset()
  listEditions.mockReset()
  hideItem.mockReset()
  unhideItem.mockReset()
  publishCandidates.mockReset()
  recoveryStatus.mockReset()
  recoveryStatus.mockResolvedValue({ dependencies: [] })
  redirectExpired.mockReset()
  redirectExpired.mockResolvedValue(false)
})

describe("NewsManagementPage", () => {
  it("retains curation data on refresh failure and offers a manual reload", async () => {
    catalog.mockResolvedValue(enabledCatalog)
    listNewsRuns.mockResolvedValue({ items: [] })
    listEditions.mockResolvedValue(editions)
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    renderPage(client)
    await screen.findByRole("heading", { name: "Published stories" })
    listEditions.mockRejectedValueOnce(
      new ApiError(500, "news-refresh-request", "failed")
    )
    await act(async () => {
      await client.refetchQueries({ queryKey: ["news-admin", "editions"] })
    })
    expect(
      screen.getByRole("heading", { name: "Published stories" })
    ).toBeInTheDocument()
    expect(
      await screen.findByText("Request ID: news-refresh-request")
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Reload" }))
    await waitFor(() =>
      expect(
        screen.queryByText("Request ID: news-refresh-request")
      ).not.toBeInTheDocument()
    )
  })
  it("shows native function status for a failed news job", async () => {
    catalog.mockResolvedValue(enabledCatalog)
    listEditions.mockResolvedValue(editions)
    listNewsRuns.mockResolvedValue({
      items: [
        jobRun({
          job_key: "news_us_equity_refresh_job",
          status: "failed",
          error: "news_recovery_required",
          functions: [
            {
              id: editionId,
              function_key: "news_us_equity_refresh",
              status: "failed",
            },
          ],
        }),
      ],
    })
    renderPage()
    expect(
      await screen.findByText("news_recovery_required")
    ).toBeInTheDocument()
  })
  it("shows an accessible loading state", () => {
    catalog.mockReturnValue(new Promise(() => {}))
    listNewsRuns.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading news management."
    )
  })

  it("requests native job run history", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      daily_news_enabled: true,
      news_markets: ["global", "tw_equity", "us_equity"],
    })
    listNewsRuns.mockResolvedValue({ items: [] })
    listEditions.mockResolvedValue(editions)
    renderPage()

    await screen.findByRole("button", { name: "Refresh all markets" })
    expect(listNewsRuns).toHaveBeenCalledOnce()
    expect(listNewsRuns).toHaveBeenCalledWith(1, undefined, "news")
  })

  it("keeps refresh controls enabled while an automatic news run is active", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      daily_news_enabled: true,
      news_markets: ["global", "tw_equity", "us_equity"],
    })
    listNewsRuns.mockResolvedValue({
      items: [
        jobRun({
          status: "running",
          requested_by_user_id: null,
          trigger: "automatic",
        }),
      ],
    })
    renderPage()

    expect(
      await screen.findByRole("button", { name: "Refresh all markets" })
    ).toBeEnabled()
    expect(
      within(marketRerunRegion()).getByRole("button", {
        name: "Taiwan equities",
      })
    ).toBeEnabled()
  })

  it("queues another refresh while a manual news run is active", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      daily_news_enabled: true,
      news_markets: ["global", "tw_equity", "us_equity"],
    })
    listNewsRuns.mockResolvedValue({
      items: [jobRun({ job_key: "news_global_refresh_job" })],
    })
    renderPage()

    expect(
      await screen.findByRole("button", { name: "Refresh all markets" })
    ).toBeEnabled()
    expect(
      within(marketRerunRegion()).getByRole("button", {
        name: "Taiwan equities",
      })
    ).toBeEnabled()
  })

  it("keeps refresh controls enabled after cancellation", async () => {
    catalog.mockResolvedValue(enabledCatalog)
    const cancelledButLeased = jobRun({
      job_key: "news_global_refresh_job",
      status: "cancelled",
      error: "cancelled_by_admin",
    })
    listNewsRuns.mockResolvedValue({
      items: [cancelledButLeased],
    })
    listEditions.mockResolvedValue(editions)

    renderPage()

    expect(
      await screen.findByRole("button", { name: "Refresh all markets" })
    ).toBeEnabled()
    expect(
      within(marketRerunRegion()).getByRole("button", {
        name: "Taiwan equities",
      })
    ).toBeEnabled()
  })

  it("queues market directly and all markets only after confirmation", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      daily_news_enabled: true,
      news_markets: ["global", "tw_equity", "us_equity"],
    })
    listNewsRuns.mockResolvedValue({ items: [] })
    listEditions.mockResolvedValue(editions)
    createRun.mockResolvedValue({})
    renderPage()
    await screen.findByRole("button", { name: "Refresh all markets" })
    fireEvent.click(
      within(marketRerunRegion()).getByRole("button", {
        name: "Taiwan equities",
      })
    )
    await waitFor(() =>
      expect(createRun).toHaveBeenCalledWith(
        "news_tw_equity_refresh_job",
        "csrf"
      )
    )
    fireEvent.click(screen.getByRole("button", { name: "Refresh all markets" }))
    expect(screen.getByRole("alertdialog")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Queue refresh" }))
    await waitFor(() =>
      expect(createRun).toHaveBeenCalledWith("news_daily_update", "csrf")
    )
  })

  it("renders candidates, counts and stage labels for the edition", async () => {
    catalog.mockResolvedValue(enabledCatalog)
    listNewsRuns.mockResolvedValue({ items: [] })
    listEditions.mockResolvedValue(editions)
    renderPage()

    expect(
      await screen.findByRole("link", { name: "Fed holds rates" })
    ).toHaveAttribute("rel", "noopener noreferrer")
    expect(listEditions).toHaveBeenCalledWith("2026-09-07")
    expect(screen.getByText("Revision 2", { exact: false })).toBeInTheDocument()
    const counts = screen.getByRole("list", { name: "Candidate counts" })
    expect(counts).toHaveTextContent("Discovered, not fetched 4")
    expect(counts).toHaveTextContent("Reviewed, not selected 3")
    expect(screen.getByText("Off market")).toBeInTheDocument()
    expect(screen.getByRole("table")).toHaveTextContent(
      "Reviewed, not selected"
    )
    expect(screen.getByText("台積電法說")).toBeInTheDocument()
    expect(
      screen.getByRole("checkbox", { name: "TSMC earnings call" })
    ).toBeDisabled()
    expect(
      screen.getByRole("button", { name: "Publish selected" })
    ).toBeDisabled()
  })

  it("hides a published item with the CSRF token and refreshes the edition", async () => {
    catalog.mockResolvedValue(enabledCatalog)
    listNewsRuns.mockResolvedValue({ items: [] })
    listEditions.mockResolvedValue(editions)
    hideItem.mockResolvedValue({ ...globalEdition.items[0], hidden: true })
    renderPage()

    fireEvent.click(
      await screen.findByRole("button", { name: "Hide: 台積電法說" })
    )

    await waitFor(() => expect(hideItem).toHaveBeenCalledWith(itemId, "csrf"))
    await waitFor(() => expect(listEditions).toHaveBeenCalledTimes(2))
    expect(unhideItem).not.toHaveBeenCalled()
  })

  it("publishes the selected candidates for the edition", async () => {
    catalog.mockResolvedValue(enabledCatalog)
    listNewsRuns.mockResolvedValue({ items: [] })
    listEditions.mockResolvedValue(editions)
    publishCandidates.mockResolvedValue({})
    renderPage()

    fireEvent.click(
      await screen.findByRole("checkbox", { name: "Fed holds rates" })
    )
    fireEvent.click(screen.getByRole("checkbox", { name: "Oil slips" }))
    const publish = screen.getByRole("button", {
      name: "Publish selected (2)",
    })
    expect(publish).toBeEnabled()
    fireEvent.click(publish)

    await waitFor(() =>
      expect(publishCandidates).toHaveBeenCalledWith(
        { edition_id: editionId, candidate_ids: [droppedId, reviewedId] },
        "csrf"
      )
    )
    await waitFor(() => expect(listEditions).toHaveBeenCalledTimes(2))
  })

  it("publishes retained candidates when automatic publication made no edition", async () => {
    catalog.mockResolvedValue(enabledCatalog)
    listNewsRuns.mockResolvedValue({ items: [] })
    listEditions.mockResolvedValue({
      ...editions,
      editions: [
        {
          market_code: "global",
          edition: null,
          items: [],
          candidates: [candidate({ stage: "reviewed", drop_reason: null })],
        },
        emptyMarket("tw_equity"),
        emptyMarket("us_equity"),
      ],
    })
    publishCandidates.mockResolvedValue({})
    renderPage()

    fireEvent.click(
      await screen.findByRole("checkbox", { name: "Fed holds rates" })
    )
    fireEvent.click(
      screen.getByRole("button", { name: "Publish selected (1)" })
    )

    await waitFor(() =>
      expect(publishCandidates).toHaveBeenCalledWith(
        {
          edition_date: "2026-09-07",
          market_code: "global",
          candidate_ids: [droppedId],
        },
        "csrf"
      )
    )
  })

  it("blocks publishing while a manual publish run is active", async () => {
    catalog.mockResolvedValue(enabledCatalog)
    listNewsRuns.mockResolvedValue({
      items: [
        jobRun({
          job_key: "news_publish_job",
          status: "running",
        }),
      ],
    })
    listEditions.mockResolvedValue({
      ...editions,
      editions: [
        {
          ...globalEdition,
          candidates: [
            candidate({
              publish_run_id: "c744cb20-bf7c-4f4a-8e7b-1e0c69a91adf",
              publish_requested_at: "2026-09-07T01:00:00+00:00",
            }),
            candidate({
              id: reviewedId,
              stage: "reviewed",
              drop_reason: null,
              headline: "Oil slips",
              url: "https://www.reuters.com/oil",
            }),
          ],
        },
      ],
    })
    renderPage()

    expect(
      await screen.findByRole("checkbox", { name: "Fed holds rates" })
    ).toBeDisabled()
    // Even with a selectable candidate ticked, the button waits for the
    // active manual publish to finish.
    const selectable = screen.getByRole("checkbox", { name: "Oil slips" })
    expect(selectable).toBeDisabled()
    expect(
      screen.getByRole("button", { name: "Publish selected" })
    ).toBeDisabled()
    expect(screen.getByText("Publish queued")).toBeInTheDocument()
    expect(
      screen.getByText("Manual publish", { exact: false })
    ).toBeInTheDocument()
  })

  it("shows an empty state when the market has no edition for the date", async () => {
    catalog.mockResolvedValue(enabledCatalog)
    listNewsRuns.mockResolvedValue({ items: [] })
    listEditions.mockResolvedValue(editions)
    renderPage()

    await screen.findByRole("link", { name: "Fed holds rates" })
    fireEvent.click(
      screen.getByRole("button", { name: "Taiwan equities", pressed: false })
    )

    expect(
      screen.getByText("No news edition exists for this market on this date.")
    ).toBeInTheDocument()
    expect(screen.queryByRole("table")).toBeNull()
  })

  it("redirects when either initial query reports an expired session", async () => {
    const expired = new ApiError(401, null, "expired")
    catalog.mockRejectedValue(expired)
    listNewsRuns.mockResolvedValue({ items: [] })
    renderPage()

    await waitFor(() => expect(redirectExpired).toHaveBeenCalledWith(expired))
  })

  it("shows an accessible error instead of an empty management state", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      daily_news_enabled: true,
      news_markets: ["global", "tw_equity", "us_equity"],
    })
    listNewsRuns.mockRejectedValue(new ApiError(500, null, "unavailable"))
    renderPage()

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unable to load news management data."
    )
    expect(
      screen.queryByRole("button", { name: "Refresh all markets" })
    ).toBeNull()
  })
})
