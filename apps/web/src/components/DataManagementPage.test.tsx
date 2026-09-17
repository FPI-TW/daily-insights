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
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { createI18n } from "#/lib/i18n"
import { DataManagementPage } from "./DataManagementPage"

const {
  catalog,
  listRuns,
  listRoutines,
  createRun,
  cancelRun,
  redirectExpired,
} = vi.hoisted(() => ({
  catalog: vi.fn(),
  listRuns: vi.fn(),
  listRoutines: vi.fn(),
  createRun: vi.fn(),
  cancelRun: vi.fn(),
  redirectExpired: vi.fn().mockResolvedValue(false),
}))

vi.mock("#/lib/admin-members", () => ({
  browserAdministrationClient: () => ({
    orchestrationCatalog: catalog,
    listJobRuns: listRuns,
    listRoutineRuns: listRoutines,
    createJobRun: createRun,
    cancelJobRun: cancelRun,
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
  registry_version: "orchestration.v1",
  registry_digest: "a".repeat(64),
  providers: [],
  functions: [],
  jobs: [],
  routine_key: "daily_market_update_v1",
  manual_market_jobs: [
    "global_macro_refresh",
    "us_equity_refresh",
    "tw_equity_refresh",
  ],
  features: { daily_news: true, analyst_viewpoints: true },
}
const emptyRuns = {
  items: [],
  page: 1,
  page_size: 10,
  total: 0,
  has_more: false,
}
const emptyRoutines = { ...emptyRuns }

function jobRun(overrides: Record<string, unknown> = {}) {
  return {
    id: "c744cb20-bf7c-4f4a-8e7b-1e0c69a91adf",
    routine_run_id: null,
    job_key: "global_macro_refresh",
    kind: "function",
    trigger: "manual",
    edition_date: "2026-09-07",
    deadline_at: null,
    status: "pending",
    requested_by_user_id: "9322a09a-6a02-421b-a966-a5cd5f44056e",
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
  listRoutines.mockReset()
  createRun.mockReset()
  cancelRun.mockReset()
  redirectExpired.mockReset()
  redirectExpired.mockResolvedValue(false)
})

beforeEach(() => {
  listRoutines.mockResolvedValue(emptyRoutines)
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

  it("offers only the three market refresh jobs", async () => {
    catalog.mockResolvedValue(catalogResult)
    listRuns.mockResolvedValue(emptyRuns)
    renderPage()
    expect(
      await screen.findByRole("button", { name: "Refresh global macro data" })
    ).toBeEnabled()
    expect(
      screen.getByRole("button", { name: "Update international indices" })
    ).toBeEnabled()
    expect(
      screen.getByRole("button", { name: "Update exchange data" })
    ).toBeEnabled()
    expect(screen.queryByRole("button", { name: "Twelve Data" })).toBeNull()
  })

  it.each([
    ["Refresh global macro data", "global_macro_refresh"],
    ["Update international indices", "us_equity_refresh"],
    ["Update exchange data", "tw_equity_refresh"],
  ])("queues %s as a native job run", async (label, key) => {
    catalog.mockResolvedValue(catalogResult)
    listRuns.mockResolvedValue(emptyRuns)
    createRun.mockResolvedValue(jobRun({ job_key: key }))
    renderPage()
    fireEvent.click(await screen.findByRole("button", { name: label }))
    await waitFor(() => expect(createRun).toHaveBeenCalledWith(key, "csrf"))
  })

  it("shows and cancels an active native job run", async () => {
    const active = jobRun({ status: "running" })
    catalog.mockResolvedValue(catalogResult)
    listRuns.mockResolvedValue({ ...emptyRuns, items: [active], total: 1 })
    cancelRun.mockResolvedValue(jobRun({ status: "cancelled" }))
    createRun.mockResolvedValue(jobRun())
    renderPage()
    const repeat = await screen.findByRole("button", {
      name: "Refresh global macro data",
    })
    expect(repeat).toBeEnabled()
    fireEvent.click(repeat)
    await waitFor(() =>
      expect(createRun).toHaveBeenCalledWith("global_macro_refresh", "csrf")
    )
    fireEvent.click(await screen.findByRole("button", { name: "Cancel run" }))
    await waitFor(() =>
      expect(cancelRun).toHaveBeenCalledWith(active.id, "csrf")
    )
  })

  it("shows the daily routine job graph, attempts and projection dependencies", async () => {
    const providerJob = jobRun({
      id: "11111111-1111-4111-8111-111111111111",
      routine_run_id: "33333333-3333-4333-8333-333333333333",
      job_key: "twelve_data_daily_update",
      trigger: "automatic",
      status: "succeeded",
      functions: [
        {
          id: "44444444-4444-4444-8444-444444444444",
          function_key: "commodity_daily_bars",
          provider_key: "twelve_data",
          scope: {},
          status: "succeeded",
          missing_scopes: null,
          attempt_count: 1,
          next_attempt_at: null,
          started_at: "2026-09-07T00:00:00+00:00",
          completed_at: "2026-09-07T00:01:00+00:00",
          result: null,
          error: null,
          attempts: [
            {
              id: "55555555-5555-4555-8555-555555555555",
              attempt_number: 1,
              status: "succeeded",
            },
          ],
          depends_on: [],
        },
      ],
      downstream_jobs: ["22222222-2222-4222-8222-222222222222"],
    })
    const projection = jobRun({
      id: "22222222-2222-4222-8222-222222222222",
      routine_run_id: "33333333-3333-4333-8333-333333333333",
      job_key: "market_reports_publish",
      kind: "projection",
      trigger: "automatic",
      status: "running",
      depends_on: [providerJob.id],
    })
    catalog.mockResolvedValue(catalogResult)
    listRuns.mockResolvedValue(emptyRuns)
    listRoutines.mockResolvedValue({
      ...emptyRoutines,
      total: 1,
      items: [
        {
          id: "33333333-3333-4333-8333-333333333333",
          routine_key: "daily_market_update_v1",
          registry_version: "orchestration.v1",
          edition_date: "2026-09-07",
          scheduled_for: "2026-09-07T00:00:00+00:00",
          deadline_at: "2026-09-07T02:00:00+00:00",
          status: "running",
          started_at: "2026-09-07T00:00:00+00:00",
          completed_at: null,
          result: null,
          created_at: "2026-09-07T00:00:00+00:00",
          jobs: [providerJob, projection],
        },
      ],
    })
    renderPage()

    expect(
      await screen.findByText("daily_market_update_v1", { exact: false })
    ).toBeInTheDocument()
    fireEvent.click(
      screen.getByText("daily_market_update_v1", { exact: false })
    )
    expect(
      screen.getByText(/^function · twelve_data_daily_update · succeeded$/)
    ).toBeInTheDocument()
    expect(
      screen.getByText("commodity_daily_bars", { exact: false })
    ).toBeInTheDocument()
    expect(screen.getByText("Attempt 1 · succeeded")).toBeInTheDocument()
    expect(
      screen.getByText("Depends on: twelve_data_daily_update")
    ).toBeInTheDocument()
  })

  it("redirects expired mutations", async () => {
    const expired = new ApiError(401, null, "expired")
    catalog.mockResolvedValue(catalogResult)
    listRuns.mockResolvedValue(emptyRuns)
    createRun.mockRejectedValue(expired)
    renderPage()
    fireEvent.click(
      await screen.findByRole("button", { name: "Update exchange data" })
    )
    await waitFor(() => expect(redirectExpired).toHaveBeenCalledWith(expired))
  })
})
