import { ApiError } from "@daily-insights/api-client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { act } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import { DataManagementPage } from "./DataManagementPage"

const { catalog, listRuns, createRun, redirectExpired } = vi.hoisted(() => ({
  catalog: vi.fn(),
  listRuns: vi.fn(),
  createRun: vi.fn(),
  redirectExpired: vi.fn().mockResolvedValue(false),
}))

vi.mock("#/lib/admin-members", () => ({
  browserAdministrationClient: () => ({
    dataManagementCatalog: catalog,
    listDataManagementRuns: listRuns,
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
  redirectExpired.mockReset()
  redirectExpired.mockResolvedValue(false)
  vi.useRealTimers()
})

describe("DataManagementPage", () => {
  it("renders its accessible initial skeleton inside QueryClientProvider", () => {
    catalog.mockReturnValue(new Promise(() => {}))
    listRuns.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading data management."
    )
  })

  it("opens alertdialog and cancel does not enqueue", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      morning_reports_enabled: true,
      yfinance_enabled: true,
      markets: ["crypto"],
    })
    listRuns.mockResolvedValue({ items: [] })
    renderPage()
    expect(
      await screen.findByRole("button", { name: "Rerun all markets" })
    ).toBeEnabled()
    fireEvent.click(screen.getByRole("button", { name: "Rerun all markets" }))
    expect(screen.getByRole("alertdialog")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }))
    expect(createRun).not.toHaveBeenCalled()
  })

  it("does not enqueue when confirmation is escaped or its backdrop is clicked", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      morning_reports_enabled: true,
      yfinance_enabled: true,
      markets: ["crypto"],
    })
    listRuns.mockResolvedValue({ items: [] })
    renderPage()
    const trigger = await screen.findByRole("button", {
      name: "Rerun all markets",
    })
    fireEvent.click(trigger)
    fireEvent.keyDown(document, { key: "Escape" })
    expect(createRun).not.toHaveBeenCalled()
    await waitFor(() => expect(trigger).toHaveFocus())
    fireEvent.click(trigger)
    fireEvent.mouseDown(screen.getByRole("presentation"))
    expect(createRun).not.toHaveBeenCalled()
  })

  it("submits a confirmed full rerun only once while its mutation is pending", async () => {
    let resolve: (value: object) => void = () => undefined
    createRun.mockReturnValue(
      new Promise<object>(done => {
        resolve = done
      })
    )
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      morning_reports_enabled: true,
      yfinance_enabled: true,
      markets: ["crypto"],
    })
    listRuns.mockResolvedValue({ items: [] })
    renderPage()
    fireEvent.click(
      await screen.findByRole("button", { name: "Rerun all markets" })
    )
    const confirm = screen.getByRole("button", { name: "Queue rerun" })
    fireEvent.click(confirm)
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(1))
    expect(confirm).toBeDisabled()
    resolve({})
  })

  it("submits single-market and Yahoo operations directly", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      morning_reports_enabled: true,
      yfinance_enabled: true,
      markets: ["crypto"],
    })
    listRuns.mockResolvedValue({ items: [] })
    createRun.mockResolvedValue({})
    renderPage()
    fireEvent.click(await screen.findByRole("button", { name: "Crypto" }))
    await waitFor(() =>
      expect(createRun).toHaveBeenCalledWith(
        { operation: "morning_market", market_code: "crypto" },
        "csrf"
      )
    )
    fireEvent.click(screen.getByRole("button", { name: "Update indices" }))
    await waitFor(() =>
      expect(createRun).toHaveBeenCalledWith(
        { operation: "index_yahoo" },
        "csrf"
      )
    )
  })

  it("renders closed structured run details with provenance and sanitized errors", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      morning_reports_enabled: true,
      yfinance_enabled: true,
      markets: ["crypto"],
    })
    listRuns.mockResolvedValue({
      items: [
        {
          id: "run-1",
          status: "partial",
          operation: "morning_all",
          market_code: null,
          edition_date: "2026-09-07",
          completed_at: "2026-09-07T00:00:00Z",
          result: {
            markets: [
              {
                market_code: "crypto",
                publication_action: "published",
                revision: 3,
                report_status: "partial",
                source_date: "2026-09-07",
                datasets: [
                  {
                    dataset_key: "crypto_prices",
                    status: "failed",
                    fetched_at: "2026-09-07T00:00:00Z",
                    source_as_of: "2026-09-06",
                    record_count: 0,
                    error: "runtimeerror",
                  },
                ],
              },
            ],
          },
          error: null,
        },
      ],
    })
    renderPage()
    const details = await screen.findByText(/partial · morning_all/)
    const container = details.closest("details")
    expect(container).not.toHaveAttribute("open")
    fireEvent.click(details)
    expect(screen.getByText(/report_status: partial/)).toBeInTheDocument()
    expect(
      screen.getByText(/fetched_at: 2026-09-07T00:00:00Z/)
    ).toBeInTheDocument()
    expect(screen.getByText(/source_as_of: 2026-09-06/)).toBeInTheDocument()
    expect(screen.getByText(/record_count: 0/)).toBeInTheDocument()
    expect(screen.getByText(/error: runtimeerror/)).toBeInTheDocument()
  })

  it("polls every two seconds only while a run is active and recovers active state after reload", async () => {
    vi.useFakeTimers()
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      morning_reports_enabled: true,
      yfinance_enabled: true,
      markets: ["crypto"],
    })
    const active = {
      items: [
        {
          id: "pending-run",
          status: "pending",
          operation: "morning_all",
          market_code: null,
          edition_date: "2026-09-07",
          completed_at: null,
          result: null,
          error: null,
        },
      ],
    }
    listRuns.mockResolvedValueOnce(active).mockResolvedValueOnce({
      items: [{ ...active.items[0], status: "succeeded" }],
    })
    renderPage()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    const full = screen.getByRole("button", {
      name: "Rerun all markets",
    })
    expect(full).toBeDisabled()
    expect(screen.getByRole("button", { name: "Crypto" })).toBeDisabled()
    expect(screen.getByRole("button", { name: "Update indices" })).toBeEnabled()
    expect(
      screen.getByText(/pending · morning_all/).closest("details")
    ).not.toHaveAttribute("open")
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000)
    })
    expect(listRuns).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000)
    })
    expect(listRuns).toHaveBeenCalledTimes(2)
  })

  it("locks only the matching operation class and reports conflict, unavailable, and session expiry distinctly", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      morning_reports_enabled: true,
      yfinance_enabled: true,
      markets: ["crypto"],
    })
    listRuns.mockResolvedValue({
      items: [
        {
          id: "index-run",
          status: "running",
          operation: "index_yahoo",
          market_code: null,
          edition_date: "2026-09-07",
          completed_at: null,
          result: null,
          error: null,
        },
      ],
    })
    createRun.mockRejectedValueOnce(new ApiError(409, null, "conflict"))
    renderPage()
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Update indices" })
      ).toBeDisabled()
    )
    expect(
      screen.getByRole("button", { name: "Rerun all markets" })
    ).toBeEnabled()
    fireEvent.click(screen.getByRole("button", { name: "Crypto" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("already active")
    createRun.mockRejectedValueOnce(new ApiError(503, null, "unavailable"))
    fireEvent.click(screen.getByRole("button", { name: "Crypto" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("unavailable")
    createRun.mockRejectedValueOnce(new ApiError(401, null, "expired"))
    fireEvent.click(screen.getByRole("button", { name: "Crypto" }))
    await waitFor(() =>
      expect(redirectExpired).toHaveBeenCalledWith(expect.any(ApiError))
    )
  })

  it("enqueues the Taiwan institutional rerun and reports what it covered", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      morning_reports_enabled: true,
      yfinance_enabled: true,
      twse_enabled: true,
      markets: ["crypto"],
    })
    listRuns.mockResolvedValue({
      items: [
        {
          id: "twse-run",
          status: "partial",
          operation: "institutional_twse",
          market_code: null,
          edition_date: "2026-09-07",
          completed_at: "2026-09-07T09:00:00Z",
          result: {
            stock_flows: {
              lookback_trading_days: 7,
              covered_trading_days: 7,
              aborted: false,
              days: [
                { trade_date: "2026-09-05", status: "no_data" },
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
    createRun.mockResolvedValue({})
    renderPage()
    fireEvent.click(
      await screen.findByRole("button", { name: "Update institutional flows" })
    )
    await waitFor(() =>
      expect(createRun).toHaveBeenCalledWith(
        { operation: "institutional_twse" },
        "csrf"
      )
    )
    // The run detail says how much of each window was actually covered, which
    // is what tells an operator whether to run it again.
    expect(
      screen.getByText(/stock_flows · covered_trading_days: 7 \/ 7/)
    ).toBeInTheDocument()
    expect(
      screen.getByText(/market_flows · covered_trading_days: 39 \/ 40/)
    ).toBeInTheDocument()
    expect(
      screen.getByText(/2026-09-04 · stored · record_count: 6700/)
    ).toBeInTheDocument()
    expect(
      screen.getByText(/2026-09-04 · failed · error: boom/)
    ).toBeInTheDocument()
  })

  it("disables the Taiwan rerun when TWSE is off, and its own run locks only itself", async () => {
    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      morning_reports_enabled: true,
      yfinance_enabled: true,
      twse_enabled: false,
      markets: ["crypto"],
    })
    listRuns.mockResolvedValue({ items: [] })
    renderPage()
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Update institutional flows" })
      ).toBeDisabled()
    )
    cleanup()

    catalog.mockResolvedValue({
      taipei_date: "2026-09-07",
      morning_reports_enabled: true,
      yfinance_enabled: true,
      twse_enabled: true,
      markets: ["crypto"],
    })
    listRuns.mockResolvedValue({
      items: [
        {
          id: "twse-run",
          status: "running",
          operation: "institutional_twse",
          market_code: null,
          edition_date: "2026-09-07",
          completed_at: null,
          result: null,
          error: null,
        },
      ],
    })
    renderPage()
    // The API locks the three operation classes separately, so a running
    // institutional job must not disable the morning or index buttons.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Update institutional flows" })
      ).toBeDisabled()
    )
    expect(
      screen.getByRole("button", { name: "Rerun all markets" })
    ).toBeEnabled()
    expect(screen.getByRole("button", { name: "Crypto" })).toBeEnabled()
    expect(screen.getByRole("button", { name: "Update indices" })).toBeEnabled()
  })
})
