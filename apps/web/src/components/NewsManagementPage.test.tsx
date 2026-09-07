import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { ApiError } from "@daily-insights/api-client"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import { createI18n } from "#/lib/i18n"
import { NewsManagementPage } from "./NewsManagementPage"

const { catalog, listRuns, listNewsRuns, createRun, redirectExpired } =
  vi.hoisted(() => ({
    catalog: vi.fn(),
    listRuns: vi.fn(),
    listNewsRuns: vi.fn(),
    createRun: vi.fn(),
    redirectExpired: vi.fn().mockResolvedValue(false),
  }))

vi.mock("#/lib/admin-members", () => ({
  browserAdministrationClient: () => ({
    dataManagementCatalog: catalog,
    listDataManagementRuns: listRuns,
    listNewsDataManagementRuns: listNewsRuns,
    createDataManagementRun: createRun,
  }),
}))
vi.mock("#/lib/auth", () => ({
  requireCsrfToken: vi.fn().mockResolvedValue("csrf"),
}))
vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => redirectExpired,
}))

function renderPage() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <I18nextProvider i18n={createI18n("en")}>
        <NewsManagementPage locale="en" />
      </I18nextProvider>
    </QueryClientProvider>
  )
}

afterEach(() => {
  cleanup()
  catalog.mockReset()
  listRuns.mockReset()
  listNewsRuns.mockReset()
  createRun.mockReset()
  redirectExpired.mockReset()
  redirectExpired.mockResolvedValue(false)
})

describe("NewsManagementPage", () => {
  it("shows an accessible loading state", () => {
    catalog.mockReturnValue(new Promise(() => {}))
    listNewsRuns.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading news management."
    )
  })

  it("requests the server-filtered news run history", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      daily_news_enabled: true,
      news_markets: ["global", "tw_equity", "us_equity"],
    })
    listNewsRuns.mockResolvedValue({ items: [] })
    renderPage()

    await screen.findByRole("button", { name: "Refresh all markets" })
    expect(listNewsRuns).toHaveBeenCalledOnce()
    expect(listRuns).not.toHaveBeenCalled()
  })

  it("keeps refresh controls disabled while a filtered news run is active", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      daily_news_enabled: true,
      news_markets: ["global", "tw_equity", "us_equity"],
    })
    listNewsRuns.mockResolvedValue({
      items: [
        {
          id: "c744cb20-bf7c-4f4a-8e7b-1e0c69a91adf",
          operation: "news_all",
          status: "running",
          edition_date: "2026-09-07",
          error: null,
          result: null,
        },
      ],
    })
    renderPage()

    expect(
      await screen.findByRole("button", { name: "Refresh all markets" })
    ).toBeDisabled()
    expect(
      screen.getByRole("button", { name: "Taiwan equities" })
    ).toBeDisabled()
  })

  it("queues market directly and all markets only after confirmation", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      daily_news_enabled: true,
      news_markets: ["global", "tw_equity", "us_equity"],
    })
    listNewsRuns.mockResolvedValue({ items: [] })
    createRun.mockResolvedValue({})
    renderPage()
    fireEvent.click(
      await screen.findByRole("button", { name: "Taiwan equities" })
    )
    await waitFor(() =>
      expect(createRun).toHaveBeenCalledWith(
        { operation: "news_market", market_code: "tw_equity" },
        "csrf"
      )
    )
    fireEvent.click(screen.getByRole("button", { name: "Refresh all markets" }))
    expect(screen.getByRole("alertdialog")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Queue refresh" }))
    await waitFor(() =>
      expect(createRun).toHaveBeenCalledWith({ operation: "news_all" }, "csrf")
    )
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
