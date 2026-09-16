import { ApiError } from "@daily-insights/api-client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act } from "react"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import { DataManagementPage } from "./DataManagementPage"

const { catalog, listRuns, createRun, cancelRun, redirectExpired } = vi.hoisted(
  () => ({
    catalog: vi.fn(),
    listRuns: vi.fn(),
    createRun: vi.fn(),
    cancelRun: vi.fn(),
    redirectExpired: vi.fn().mockResolvedValue(false),
  })
)

vi.mock("#/lib/admin-members", () => ({
  browserAdministrationClient: () => ({
    dataManagementCatalog: catalog,
    listDataManagementRuns: listRuns,
    createDataManagementRun: createRun,
    cancelDataManagementRun: cancelRun,
  }),
}))
vi.mock("#/lib/auth", () => ({
  requireCsrfToken: vi.fn().mockResolvedValue("csrf"),
}))
vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => redirectExpired,
}))

const catalogResult = {
  taipei_date: "2026-09-07",
  morning_reports_enabled: true,
  yfinance_enabled: true,
  twse_enabled: true,
  daily_news_enabled: true,
  markets: ["global_macro_bonds", "crypto", "us_equity"],
  rerunnable_providers: ["twelve_data", "yahoo_finance", "twse"],
  news_markets: ["global", "tw_equity", "us_equity"],
  macro_dashboard_enabled: true,
}
const emptyRuns = {
  items: [],
  page: 1,
  page_size: 10,
  total: 0,
  has_more: false,
  active_runs: [],
  current_day_runs: [],
}

function renderPage() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <I18nextProvider i18n={createI18n("en")}>
        <DataManagementPage locale="en" />
      </I18nextProvider>
    </QueryClientProvider>
  )
}

afterEach(() => {
  cleanup()
  catalog.mockReset()
  listRuns.mockReset()
  createRun.mockReset()
  cancelRun.mockReset()
  redirectExpired.mockReset()
  redirectExpired.mockResolvedValue(false)
  vi.useRealTimers()
})

describe("DataManagementPage", () => {
  it("renders an accessible initial loading state", () => {
    catalog.mockReturnValue(new Promise(() => {}))
    listRuns.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading data management."
    )
  })

  it("shows exactly the full and single-provider rerun blocks", async () => {
    catalog.mockResolvedValue(catalogResult)
    listRuns.mockResolvedValue(emptyRuns)
    renderPage()
    expect(await screen.findByText("Full morning rerun")).toBeInTheDocument()
    expect(screen.getByText("Single-provider rerun")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Twelve Data" })).toBeEnabled()
    expect(screen.getByRole("button", { name: "Yahoo Finance" })).toBeEnabled()
    expect(screen.getByRole("button", { name: "TWSE" })).toBeEnabled()
    expect(screen.queryByText("Single-market rerun")).not.toBeInTheDocument()

    const full = screen.getByRole("button", {
      name: "Rerun full morning report",
    })
    fireEvent.click(full)
    expect(screen.getByRole("alertdialog")).toHaveTextContent(
      "Twelve Data, Yahoo Finance, TWSE"
    )
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }))
    expect(createRun).not.toHaveBeenCalled()
    await waitFor(() => expect(full).toHaveFocus())
  })

  it("submits the confirmed full rerun only once while pending", async () => {
    let resolve: (value: object) => void = () => undefined
    createRun.mockReturnValue(new Promise<object>(done => (resolve = done)))
    catalog.mockResolvedValue(catalogResult)
    listRuns.mockResolvedValue(emptyRuns)
    renderPage()
    fireEvent.click(
      await screen.findByRole("button", { name: "Rerun full morning report" })
    )
    const confirm = screen.getByRole("button", { name: "Queue rerun" })
    fireEvent.click(confirm)
    await waitFor(() =>
      expect(createRun).toHaveBeenCalledWith(
        { operation: "morning_all" },
        "csrf"
      )
    )
    expect(confirm).toBeDisabled()
    resolve({})
  })

  it("queues each provider using the provider contract", async () => {
    catalog.mockResolvedValue(catalogResult)
    listRuns.mockResolvedValue(emptyRuns)
    createRun.mockResolvedValue({})
    renderPage()
    for (const [label, provider] of [
      ["Twelve Data", "twelve_data"],
      ["Yahoo Finance", "yahoo_finance"],
      ["TWSE", "twse"],
    ] as const) {
      fireEvent.click(await screen.findByRole("button", { name: label }))
      await waitFor(() =>
        expect(createRun).toHaveBeenCalledWith(
          { operation: "provider_rerun", provider },
          "csrf"
        )
      )
    }
    expect(createRun).toHaveBeenCalledTimes(3)
  })

  it("gates providers independently and requires all providers for a full run", async () => {
    catalog.mockResolvedValue({ ...catalogResult, yfinance_enabled: false })
    listRuns.mockResolvedValue(emptyRuns)
    renderPage()
    expect(
      await screen.findByRole("button", { name: "Yahoo Finance" })
    ).toBeDisabled()
    expect(screen.getByRole("button", { name: "Twelve Data" })).toBeEnabled()
    expect(screen.getByRole("button", { name: "TWSE" })).toBeEnabled()
    expect(
      screen.getByRole("button", { name: "Rerun full morning report" })
    ).toBeDisabled()
  })

  it("blocks full reruns for active provider work and only the matching provider", async () => {
    catalog.mockResolvedValue(catalogResult)
    const active = {
      id: "provider-run",
      status: "running",
      operation: "provider_rerun",
      provider: "twse",
      market_code: null,
      edition_date: "2026-09-07",
      requested_by_user_id: "admin",
      created_at: "2026-09-07T00:00:00Z",
      started_at: "2026-09-07T00:01:00Z",
      completed_at: null,
      result: null,
      error: null,
    }
    listRuns.mockResolvedValue({
      ...emptyRuns,
      items: [active],
      active_runs: [active],
    })
    renderPage()
    expect(
      await screen.findByRole("button", { name: "Rerun full morning report" })
    ).toBeDisabled()
    expect(screen.getByRole("button", { name: "TWSE" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Twelve Data" })).toBeEnabled()
    expect(screen.getByRole("button", { name: "Yahoo Finance" })).toBeEnabled()
  })

  it("keeps matching controls blocked for cancelled work that still has a lease", async () => {
    catalog.mockResolvedValue(catalogResult)
    const cancelledButLeased = {
      id: "cancelled-provider-run",
      status: "cancelled",
      operation: "provider_rerun",
      provider: "twse",
      market_code: null,
      edition_date: "2026-09-07",
      requested_by_user_id: "admin",
      created_at: "2026-09-07T00:00:00Z",
      started_at: "2026-09-07T00:01:00Z",
      completed_at: "2026-09-07T00:02:00Z",
      result: null,
      error: "cancelled_by_admin",
    }
    listRuns.mockResolvedValue({
      ...emptyRuns,
      items: [cancelledButLeased],
      active_runs: [cancelledButLeased],
    })

    renderPage()

    expect(
      await screen.findByRole("button", { name: "Rerun full morning report" })
    ).toBeDisabled()
    expect(screen.getByRole("button", { name: "TWSE" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Twelve Data" })).toBeEnabled()
    expect(screen.getByRole("button", { name: "Yahoo Finance" })).toBeEnabled()
  })

  it("renders provider-separated full-run details", async () => {
    catalog.mockResolvedValue(catalogResult)
    listRuns.mockResolvedValue({
      ...emptyRuns,
      items: [
        {
          id: "full-run",
          status: "partial",
          operation: "morning_all",
          market_code: null,
          edition_date: "2026-09-07",
          completed_at: "2026-09-07T09:00:00Z",
          result: {
            providers: {
              twelve_data: {
                status: "succeeded",
                details: { markets: [] },
                error: null,
              },
              yahoo_finance: {
                status: "partial",
                details: { symbols: [{ symbol: "^GSPC", status: "failed" }] },
                error: "index_partial",
              },
              twse: {
                status: "failed",
                details: {},
                error: "twse_unavailable",
              },
            },
          },
          error: "full_morning_failures",
        },
      ],
    })
    renderPage()
    fireEvent.click(await screen.findByText(/partial · morning_all/))
    expect(screen.getByText(/Twelve Data · succeeded/)).toBeInTheDocument()
    expect(screen.getByText(/Yahoo Finance · partial/)).toBeInTheDocument()
    expect(screen.getByText(/TWSE · failed/)).toBeInTheDocument()
    expect(screen.getByText(/error: index_partial/)).toBeInTheDocument()
  })

  it("renders TWSE provider coverage details", async () => {
    catalog.mockResolvedValue(catalogResult)
    listRuns.mockResolvedValue({
      ...emptyRuns,
      items: [
        {
          id: "twse-run",
          status: "partial",
          operation: "provider_rerun",
          provider: "twse",
          market_code: null,
          edition_date: "2026-09-07",
          completed_at: "2026-09-07T09:00:00Z",
          result: {
            index: { symbol: "^TWII", status: "succeeded", record_count: 21 },
            stock_flows: {
              lookback_trading_days: 1,
              covered_trading_days: 1,
              aborted: false,
              days: [
                {
                  trade_date: "2026-09-04",
                  status: "stored",
                  record_count: 6700,
                },
              ],
            },
            market_flows: {
              lookback_trading_days: 40,
              covered_trading_days: 39,
              aborted: false,
              days: [
                { trade_date: "2026-09-04", status: "failed", error: "boom" },
              ],
            },
          },
          error: null,
        },
      ],
    })
    renderPage()
    expect(
      await screen.findByText("Covered 1 / 1 trading days")
    ).toBeInTheDocument()
    expect(screen.getByText("Covered 39 / 40 trading days")).toBeInTheDocument()
    expect(screen.getByText("6700")).toBeInTheDocument()
    expect(screen.getByText("boom")).toBeInTheDocument()
    expect(
      screen.getByText(/TWSE · \^TWII · Taiwan Weighted Index/)
    ).toBeInTheDocument()
  })

  it("paginates history while keeping off-page active runs visible", async () => {
    catalog.mockResolvedValue(catalogResult)
    const active = {
      id: "active-run",
      status: "running",
      operation: "provider_rerun",
      provider: "twse",
      market_code: null,
      edition_date: "2026-09-07",
      requested_by_user_id: "admin",
      created_at: "2026-09-07T00:00:00Z",
      started_at: null,
      completed_at: null,
      result: null,
      error: null,
    }
    listRuns
      .mockResolvedValueOnce({
        ...emptyRuns,
        total: 11,
        has_more: true,
        active_runs: [active],
      })
      .mockResolvedValue({
        ...emptyRuns,
        page: 2,
        total: 11,
        active_runs: [active],
      })
    renderPage()
    expect(await screen.findByText("Active runs")).toBeInTheDocument()
    expect(
      screen.getByText(/running · provider_rerun · TWSE/)
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Next" }))
    await waitFor(() => expect(listRuns).toHaveBeenLastCalledWith(2))
    expect(await screen.findByText("Page 2")).toBeInTheDocument()
  })

  it("polls only while the API reports an active run", async () => {
    vi.useFakeTimers()
    catalog.mockResolvedValue(catalogResult)
    const pending = {
      id: "pending-run",
      status: "pending",
      operation: "morning_all",
      market_code: null,
      edition_date: "2026-09-07",
      completed_at: null,
      result: null,
      error: null,
    }
    listRuns
      .mockResolvedValueOnce({
        ...emptyRuns,
        items: [pending],
        total: 1,
        active_runs: [pending],
      })
      .mockResolvedValue({
        ...emptyRuns,
        items: [{ ...pending, status: "succeeded" }],
        total: 1,
      })
    renderPage()
    await act(async () => vi.advanceTimersByTimeAsync(0))
    expect(
      screen.getByRole("button", { name: "Rerun full morning report" })
    ).toBeDisabled()
    expect(screen.getByRole("button", { name: "Twelve Data" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Yahoo Finance" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "TWSE" })).toBeDisabled()
    await act(async () => vi.advanceTimersByTimeAsync(2_000))
    expect(listRuns).toHaveBeenCalledTimes(2)
    await act(async () => vi.advanceTimersByTimeAsync(2_000))
    expect(listRuns).toHaveBeenCalledTimes(2)
  })

  it("reports conflict, unavailable, and session expiry distinctly", async () => {
    catalog.mockResolvedValue(catalogResult)
    listRuns.mockResolvedValue(emptyRuns)
    createRun.mockRejectedValueOnce(new ApiError(409, null, "conflict"))
    renderPage()
    const twse = await screen.findByRole("button", { name: "TWSE" })
    fireEvent.click(twse)
    expect(await screen.findByRole("alert")).toHaveTextContent("already active")
    createRun.mockRejectedValueOnce(new ApiError(503, null, "unavailable"))
    fireEvent.click(twse)
    expect(await screen.findByRole("alert")).toHaveTextContent("unavailable")
    createRun.mockRejectedValueOnce(new ApiError(401, null, "expired"))
    fireEvent.click(twse)
    await waitFor(() =>
      expect(redirectExpired).toHaveBeenCalledWith(expect.any(ApiError))
    )
  })
})
