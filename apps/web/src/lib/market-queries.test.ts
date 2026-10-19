import { ApiError } from "@daily-insights/api-client"
import { QueryClient } from "@tanstack/react-query"
import { afterEach, describe, expect, it, vi } from "vitest"
import {
  marketQueries,
  marketQueryKey,
  marketQueryPolicy,
  marketTransport,
} from "./market-queries"
import { clearMarketQueries } from "./useSessionExpiry"

const scope = { id: "user-a", organization_id: "org-a" }
const range = { start: "2024-10-05", end: "2026-10-05" }
const bar = {
  symbol: "^DJI",
  market_code: "us_equity",
  trade_date: "2026-10-02",
  open: "100",
  high: "102",
  low: "99",
  close: "101",
  adjusted_close: null,
  volume: 10,
}
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  })
afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe("market browser queries", () => {
  it("uses same-origin credentials, locale and Query cancellation", async () => {
    const fetch = vi.fn().mockResolvedValue(json([]))
    vi.stubGlobal("fetch", fetch)
    const controller = new AbortController()
    await marketQueries(scope, "zh-hant")
      .reports()
      .queryFn({ signal: controller.signal })
    expect(fetch).toHaveBeenCalledWith(
      "/api/reports?locale=zh-hant",
      expect.objectContaining({
        credentials: "same-origin",
        signal: expect.any(AbortSignal),
      })
    )
    const stalled = vi.fn(
      (_path: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) =>
          init.signal?.addEventListener("abort", () =>
            reject(init.signal?.reason)
          )
        )
    )
    vi.stubGlobal("fetch", stalled)
    const request = marketTransport(controller.signal)("/api/markets")
    const rejected = expect(request).rejects.toBeDefined()
    controller.abort()
    await rejected
    expect(stalled.mock.calls[0]?.[1].signal?.aborted).toBe(true)
  })
  it("times out charts at 10 seconds and macro at 60 seconds, including response bodies", async () => {
    vi.useFakeTimers()
    const fetch = vi.fn((_path: string, init: RequestInit) =>
      Promise.resolve({
        arrayBuffer: () =>
          new Promise<ArrayBuffer>((_resolve, reject) =>
            init.signal?.addEventListener("abort", () =>
              reject(init.signal?.reason)
            )
          ),
      })
    )
    vi.stubGlobal("fetch", fetch)
    const signal = new AbortController().signal
    const chart = marketQueries(scope, "en").vix(range).queryFn({ signal })
    const macro = marketQueries(scope, "en").macro().queryFn({ signal })
    const chartRejected = expect(chart).rejects.toHaveProperty(
      "name",
      "TimeoutError"
    )
    const macroRejected = expect(macro).rejects.toHaveProperty(
      "name",
      "TimeoutError"
    )
    await vi.advanceTimersByTimeAsync(10_000)
    await chartRejected
    expect(fetch.mock.calls[1]?.[1].signal?.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(50_000)
    await macroRejected
  })
  it("keeps successful symbol outcomes and propagates a 401 from aggregation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((path: string) =>
        Promise.resolve(
          path.includes("%5EDJI")
            ? json([bar])
            : json({ detail: "unavailable" }, 503)
        )
      )
    )
    const options = marketQueries(scope, "en").history("us_equity", range)
    const result = await options.queryFn({
      signal: new AbortController().signal,
    })
    expect(result.series).toHaveLength(1)
    expect(result.failedSymbols.length).toBeGreaterThan(0)
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(json({ detail: "expired" }, 401)))
    )
    await expect(
      options.queryFn({ signal: new AbortController().signal })
    ).rejects.toBeInstanceOf(ApiError)
    await expect(
      options.queryFn({ signal: new AbortController().signal })
    ).rejects.toHaveProperty("status", 401)
  })
  it("isolates user, organization, locale and range; shares viewpoints across locales", () => {
    const en = marketQueries(scope, "en")
    expect(en.reports().queryKey).not.toEqual(
      marketQueries(scope, "zh-hant").reports().queryKey
    )
    expect(en.markets().queryKey).toEqual(
      marketQueries(scope, "en").markets().queryKey
    )
    expect(en.viewpoints().queryKey).toEqual(
      marketQueries(scope, "zh-hant").viewpoints().queryKey
    )
    expect(en.history("tw_equity", range).queryKey).not.toEqual(
      en.history("tw_equity", { ...range, end: "2026-10-04" }).queryKey
    )
    expect(marketQueryKey(scope, "reports")).not.toEqual(
      marketQueryKey({ ...scope, id: "user-b" }, "reports")
    )
    expect(marketQueryKey(scope, "reports")).not.toEqual(
      marketQueryKey({ ...scope, organization_id: "org-b" }, "reports")
    )
  })
  it("reuses 60-second cache and preserves cached data when stale refetch fails", async () => {
    let now = Date.now()
    vi.spyOn(Date, "now").mockImplementation(() => now)
    const client = new QueryClient()
    const queryFn = vi.fn().mockResolvedValue(["cached"])
    const options = {
      ...marketQueryPolicy,
      queryKey: marketQueryKey(scope, "test"),
      queryFn,
    }
    await client.fetchQuery(options)
    now += 59_999
    await client.fetchQuery(options)
    expect(queryFn).toHaveBeenCalledTimes(1)
    now += 2
    queryFn.mockRejectedValueOnce(new Error("offline"))
    await expect(client.fetchQuery(options)).rejects.toThrow("offline")
    expect(client.getQueryData(options.queryKey)).toEqual(["cached"])
    client.setQueryData(["unrelated"], "keep")
    clearMarketQueries(client)
    expect(client.getQueryData(options.queryKey)).toBeUndefined()
    expect(client.getQueryData(["unrelated"])).toBe("keep")
    client.clear()
    vi.restoreAllMocks()
  })
  it("validates report_not_generated discrimination and internal-user 403 preview", async () => {
    const options = marketQueries(scope, "en")
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          json({ detail: { code: "report_not_generated" } }, 404)
        )
    )
    await expect(
      options
        .report("us_equity")
        .queryFn({ signal: new AbortController().signal })
    ).resolves.toMatchObject({ kind: "not-generated" })
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(json({ detail: "report_not_generated" }, 404))
    )
    await expect(
      options
        .report("us_equity")
        .queryFn({ signal: new AbortController().signal })
    ).resolves.toMatchObject({ kind: "not-found" })
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(json({ detail: "no organization" }, 403)))
    )
    await expect(
      options.markets().queryFn({ signal: new AbortController().signal })
    ).resolves.toContainEqual({ code: "tw_equity", name: "tw_equity" })
    await expect(
      options.reports().queryFn({ signal: new AbortController().signal })
    ).resolves.toEqual([])
  })
})
