import { ApiError } from "@daily-insights/api-client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, renderHook } from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { rememberCsrfToken } from "./auth"
import {
  clearMarketQueries,
  isSessionExpiryPending,
  useSessionExpiryRedirect,
} from "./useSessionExpiry"

const router = vi.hoisted(() => ({
  invalidate: vi.fn().mockResolvedValue(undefined),
  navigate: vi.fn().mockResolvedValue(undefined),
}))
vi.mock("@tanstack/react-router", () => ({ useRouter: () => router }))
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})
describe("market session cleanup", () => {
  it("cancels in-flight market requests and clears only market cache on logout", async () => {
    const client = new QueryClient()
    let signal!: AbortSignal
    const request = client.fetchQuery({
      queryKey: ["market", "u", "o", "history"],
      queryFn: context => {
        signal = context.signal
        return new Promise(() => {})
      },
    })
    const cancellation = expect(request).rejects.toBeDefined()
    client.setQueryData(["market", "u", "o", "news"], "private data")
    client.setQueryData(["other"], "retained")
    clearMarketQueries(client)
    expect(signal.aborted).toBe(true)
    await cancellation
    expect(client.getQueriesData({ queryKey: ["market"] })).toEqual([])
    expect(client.getQueryData(["other"])).toBe("retained")
    client.clear()
  })
  it("deduplicates simultaneous 401s and holds queries disabled until a new authenticated session", async () => {
    rememberCsrfToken("session-before-expiry")
    const client = new QueryClient()
    client.setQueryData(["market", "u", "o", "news"], "private data")
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )
    const first = renderHook(() => useSessionExpiryRedirect("en", "customer"), {
      wrapper,
    })
    const second = renderHook(
      () => useSessionExpiryRedirect("en", "customer"),
      { wrapper }
    )
    await act(async () => {
      await Promise.all([
        first.result.current(new ApiError(401, null, "expired")),
        second.result.current(new ApiError(401, null, "expired")),
      ])
    })
    expect(router.invalidate).toHaveBeenCalledTimes(1)
    expect(router.navigate).toHaveBeenCalledTimes(1)
    expect(router.navigate).toHaveBeenCalledWith({
      to: "/{-$locale}/login",
      params: { locale: "en" },
      replace: true,
    })
    expect(client.getQueriesData({ queryKey: ["market"] })).toEqual([])
    expect(isSessionExpiryPending(router)).toBe(true)
    rememberCsrfToken("new-session")
    expect(isSessionExpiryPending(router)).toBe(false)
    client.clear()
  })
})
