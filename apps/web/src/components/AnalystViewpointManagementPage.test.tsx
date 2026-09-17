import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import type { JobRun } from "@daily-insights/api-client"
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { I18nextProvider } from "react-i18next"
import { createI18n } from "#/lib/i18n"
import { AnalystViewpointManagementPage } from "./AnalystViewpointManagementPage"

const { getJobRun, invalidate, redirectExpired, syncAnalystViewpoints } =
  vi.hoisted(() => ({
    getJobRun: vi.fn(),
    invalidate: vi.fn().mockResolvedValue(undefined),
    redirectExpired: vi.fn().mockResolvedValue(false),
    syncAnalystViewpoints: vi.fn(),
  }))

vi.mock("@tanstack/react-router", () => ({
  useRouter: () => ({ invalidate }),
}))
vi.mock("#/lib/admin-members", () => ({
  browserAdministrationClient: () => ({ getJobRun, syncAnalystViewpoints }),
}))
vi.mock("#/lib/auth", () => ({
  requireCsrfToken: vi.fn().mockResolvedValue("csrf"),
}))
vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => redirectExpired,
}))

const queuedRun: JobRun = {
  id: "c744cb20-bf7c-4f4a-8e7b-1e0c69a91adf",
  routine_run_id: null,
  job_key: "analyst_viewpoints_refresh",
  kind: "function",
  trigger: "manual",
  edition_date: "2026-09-16",
  deadline_at: "2026-09-16T02:00:00+00:00",
  status: "pending",
  requested_by_user_id: "ee77eab0-3910-4706-803c-ffaf979f1ff7",
  payload: null,
  started_at: null,
  completed_at: null,
  result: null,
  error: null,
  created_at: "2026-09-16T00:00:00+00:00",
  functions: [],
  depends_on: [],
  downstream_jobs: [],
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe("AnalystViewpointManagementPage", () => {
  it("keeps sync disabled until the queued job becomes terminal", async () => {
    let acceptRun!: (value: JobRun) => void
    const accepted = new Promise<JobRun>(resolve => {
      acceptRun = resolve
    })
    let finishRun!: (value: JobRun) => void
    const terminal = new Promise<JobRun>(resolve => {
      finishRun = resolve
    })
    syncAnalystViewpoints.mockReturnValue(accepted)
    getJobRun.mockReturnValue(terminal)
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <I18nextProvider i18n={createI18n("en")}>
          <AnalystViewpointManagementPage
            locale="en"
            status={{
              enabled: true,
              today: "2026-09-16",
              viewpoints: [],
              latest_sync: null,
            }}
          />
        </I18nextProvider>
      </QueryClientProvider>
    )

    fireEvent.click(screen.getByRole("button", { name: "Sync now" }))
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled()
    )
    fireEvent.click(screen.getByRole("button", { name: "Working…" }))
    expect(syncAnalystViewpoints).toHaveBeenCalledTimes(1)
    await act(async () => acceptRun(queuedRun))
    await waitFor(() => expect(getJobRun).toHaveBeenCalledWith(queuedRun.id))
    expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled()

    await act(async () => {
      finishRun({
        ...queuedRun,
        status: "succeeded",
        completed_at: "2026-09-16T00:01:00+00:00",
      })
    })
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ sync: true }))
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Sync now" })).toBeEnabled()
    )
  })
})
