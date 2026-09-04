import { act, cleanup, fireEvent, render, screen } from "@testing-library/react"
import { I18nextProvider } from "react-i18next"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
  ApiError,
  type YfinanceDailyBarsResponse,
} from "@daily-insights/api-client"
import { createI18n } from "#/lib/i18n"
import { IndexDataManagementPage } from "./IndexDataManagementPage"

const {
  refreshIndexDailyBars,
  requireCsrfToken,
  redirectExpiredSession,
  authState,
} = vi.hoisted(() => ({
  refreshIndexDailyBars: vi.fn(),
  requireCsrfToken: vi.fn().mockResolvedValue("csrf-token"),
  redirectExpiredSession: vi.fn().mockResolvedValue(false),
  authState: { epoch: 0 },
}))

vi.mock("#/lib/admin-members", () => ({
  browserAdministrationClient: () => ({ refreshIndexDailyBars }),
}))
vi.mock("#/lib/auth", () => ({
  requireCsrfToken,
  getAuthSessionEpoch: () => authState.epoch,
}))
vi.mock("#/lib/useSessionExpiry", () => ({
  useSessionExpiryRedirect: () => redirectExpiredSession,
}))

const response: YfinanceDailyBarsResponse = {
  period: "7d",
  fetched_at: "2026-09-04T00:00:00Z",
  succeeded: [
    {
      symbol: "^TWII",
      market: "tw_equity",
      as_of: "2026-09-03",
      stored_count: 5,
      dropped_unsettled_trade_date: "2026-09-04",
    },
  ],
  failed: [
    {
      symbol: "^HSI",
      market: "hk_equity",
      error: "provider unavailable",
    },
  ],
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, reject, resolve }
}

async function renderPage() {
  const i18n = createI18n("en")
  await i18n.changeLanguage("en")
  return render(
    <I18nextProvider i18n={i18n}>
      <IndexDataManagementPage locale="en" />
    </I18nextProvider>
  )
}

beforeEach(() => {
  vi.useFakeTimers()
  refreshIndexDailyBars.mockReset()
  requireCsrfToken.mockClear()
  redirectExpiredSession.mockClear()
  redirectExpiredSession.mockResolvedValue(false)
  authState.epoch = 0
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe("IndexDataManagementPage", () => {
  it("sends one request, shows pending, and enforces the cooldown", async () => {
    const request = deferred<YfinanceDailyBarsResponse>()
    refreshIndexDailyBars.mockReturnValueOnce(request.promise)
    await renderPage()
    const button = screen.getByRole("button")

    fireEvent.click(button)
    fireEvent.click(button)

    await act(async () => {})

    expect(refreshIndexDailyBars).toHaveBeenCalledTimes(1)
    expect(refreshIndexDailyBars).toHaveBeenCalledWith("csrf-token")
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute("aria-busy", "true")

    await act(async () => request.resolve(response))

    expect(screen.getByText("^TWII")).toBeInTheDocument()
    expect(screen.getByText("^HSI")).toBeInTheDocument()
    expect(screen.getByText("Sep 4, 2026")).toBeInTheDocument()
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute("aria-busy", "false")

    act(() => vi.advanceTimersByTime(999))
    expect(button).toBeDisabled()
    act(() => vi.advanceTimersByTime(1))
    expect(button).toBeEnabled()
  })

  it("shares an in-flight request and cooldown across a remount", async () => {
    const request = deferred<YfinanceDailyBarsResponse>()
    refreshIndexDailyBars.mockReturnValueOnce(request.promise)
    const first = await renderPage()

    fireEvent.click(screen.getByRole("button"))
    await act(async () => {})
    expect(refreshIndexDailyBars).toHaveBeenCalledTimes(1)

    first.unmount()
    await renderPage()
    const remountedButton = screen.getByRole("button")
    expect(remountedButton).toBeDisabled()
    expect(remountedButton).toHaveAttribute("aria-busy", "true")
    fireEvent.click(remountedButton)
    expect(refreshIndexDailyBars).toHaveBeenCalledTimes(1)

    await act(async () => request.resolve(response))
    expect(remountedButton).toBeDisabled()
    expect(remountedButton).toHaveAttribute("aria-busy", "false")
    act(() => vi.advanceTimersByTime(1_000))
    expect(remountedButton).toBeEnabled()
  })

  it("clears a settled result after the last page unmounts", async () => {
    refreshIndexDailyBars.mockResolvedValueOnce(response)
    const page = await renderPage()
    fireEvent.click(screen.getByRole("button"))
    await act(async () => {})
    act(() => vi.advanceTimersByTime(1_000))
    expect(screen.getByText("^TWII")).toBeInTheDocument()

    page.unmount()
    await renderPage()
    expect(screen.queryByText("^TWII")).not.toBeInTheDocument()
    expect(screen.getByRole("button")).toBeEnabled()
  })

  it("does not leave a cooldown timer after an unobserved request settles", async () => {
    const request = deferred<YfinanceDailyBarsResponse>()
    refreshIndexDailyBars.mockReturnValueOnce(request.promise)
    const page = await renderPage()
    fireEvent.click(screen.getByRole("button"))
    await act(async () => {})
    page.unmount()

    await act(async () => request.resolve(response))
    expect(vi.getTimerCount()).toBe(0)
  })

  it("ignores an old 401 before the next page mounts", async () => {
    const request = deferred<YfinanceDailyBarsResponse>()
    refreshIndexDailyBars.mockReturnValueOnce(request.promise)
    const page = await renderPage()
    fireEvent.click(screen.getByRole("button"))
    await act(async () => {})
    authState.epoch += 1
    page.unmount()

    await act(async () =>
      request.reject(new ApiError(401, null, "old session expired"))
    )
    expect(redirectExpiredSession).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)

    refreshIndexDailyBars.mockResolvedValueOnce(response)
    await renderPage()
    const button = screen.getByRole("button")
    expect(button).toBeEnabled()
    fireEvent.click(button)
    await act(async () => {})
    expect(refreshIndexDailyBars).toHaveBeenCalledTimes(2)
  })

  it("clears session-expired refresh state without a cooldown", async () => {
    refreshIndexDailyBars.mockRejectedValueOnce(
      new ApiError(401, null, "session expired")
    )
    redirectExpiredSession.mockResolvedValueOnce(true)
    const page = await renderPage()
    fireEvent.click(screen.getByRole("button"))
    await act(async () => {})

    expect(redirectExpiredSession).toHaveBeenCalledTimes(1)
    expect(screen.getByRole("button")).toBeEnabled()
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()

    page.unmount()
    await renderPage()
    expect(screen.queryByText("^TWII")).not.toBeInTheDocument()
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    expect(screen.getByRole("button")).toBeEnabled()
  })

  it("ignores every old-session outcome after an epoch change", async () => {
    const oldSuccess = deferred<YfinanceDailyBarsResponse>()
    refreshIndexDailyBars.mockReturnValueOnce(oldSuccess.promise)
    const oldPage = await renderPage()
    fireEvent.click(screen.getByRole("button"))
    await act(async () => {})

    authState.epoch += 1
    oldPage.unmount()
    refreshIndexDailyBars.mockResolvedValueOnce(response)
    await renderPage()
    fireEvent.click(screen.getByRole("button"))
    await act(async () => {})
    expect(refreshIndexDailyBars).toHaveBeenCalledTimes(2)

    await act(async () => oldSuccess.resolve(response))
    expect(screen.getByText("^TWII")).toBeInTheDocument()
    expect(redirectExpiredSession).not.toHaveBeenCalled()

    cleanup()
    const oldFailure = deferred<YfinanceDailyBarsResponse>()
    refreshIndexDailyBars.mockReset()
    refreshIndexDailyBars.mockReturnValueOnce(oldFailure.promise)
    const failurePage = await renderPage()
    fireEvent.click(screen.getByRole("button"))
    await act(async () => {})
    authState.epoch += 1
    failurePage.unmount()
    refreshIndexDailyBars.mockResolvedValueOnce(response)
    await renderPage()
    fireEvent.click(screen.getByRole("button"))
    await act(async () => {})
    await act(async () => oldFailure.reject(new Error("old failure")))
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()

    cleanup()
    const oldUnauthorized = deferred<YfinanceDailyBarsResponse>()
    refreshIndexDailyBars.mockReset()
    refreshIndexDailyBars.mockReturnValueOnce(oldUnauthorized.promise)
    const unauthorizedPage = await renderPage()
    fireEvent.click(screen.getByRole("button"))
    await act(async () => {})
    authState.epoch += 1
    unauthorizedPage.unmount()
    refreshIndexDailyBars.mockResolvedValueOnce(response)
    await renderPage()
    fireEvent.click(screen.getByRole("button"))
    await act(async () => {})
    await act(async () =>
      oldUnauthorized.reject(new ApiError(401, null, "old session expired"))
    )
    expect(redirectExpiredSession).not.toHaveBeenCalled()
  })
})
