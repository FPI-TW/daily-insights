import {
  ApiError,
  createBrowserTransport,
  createMarketClient,
  createReportClient,
  createNewsClient,
  createAnalystViewpointClient,
  launchMarketCodeSchema,
  marketCodeSchema,
  type ApiTransport,
  type Locale,
  type MarketCode,
  type User,
} from "@daily-insights/api-client"
import { useQuery } from "@tanstack/react-query"
import { useHydrated, useRouter } from "@tanstack/react-router"
import { useEffect } from "react"
import { z } from "zod"
import { mapReportDetail, mapReportSummary } from "./reports"
import { marketName } from "./markets"
import {
  indexChartSymbolsForMarket,
  indexHistoryOutcomes,
  indexMovingAverageOutcomes,
  VIX_SYMBOL,
} from "./indices"
import { macroDashboardSchema } from "./macro-dashboard"
import { isNewsMarketCode } from "./provisional-reports"
import {
  isSessionExpiryPending,
  useSessionExpiryRedirect,
} from "./useSessionExpiry"

export const marketQueryPolicy = {
  staleTime: 60_000,
  gcTime: 300_000,
  retry: false,
  refetchOnMount: true,
  refetchOnWindowFocus: true,
  refetchOnReconnect: true,
  refetchInterval: false,
} as const
export type MarketScope = Pick<User, "id" | "organization_id">
export function marketQueryKey(
  scope: MarketScope,
  resource: string,
  ...dependencies: unknown[]
) {
  return [
    "market",
    scope.id,
    scope.organization_id,
    resource,
    ...dependencies,
  ] as const
}

// Every API client still validates its response. The wrapper binds Query's
// cancellation signal and a per-request deadline to the existing transport.
export function marketTransport(
  signal: AbortSignal,
  timeoutMs?: number
): ApiTransport {
  const browser = createBrowserTransport()
  return (path, init) => {
    const controller = new AbortController()
    const abort = () => controller.abort(signal.reason)
    if (signal.aborted) abort()
    else signal.addEventListener("abort", abort, { once: true })
    const timeout =
      timeoutMs === undefined
        ? undefined
        : setTimeout(
            () =>
              controller.abort(
                new DOMException("Request timed out", "TimeoutError")
              ),
            timeoutMs
          )
    return browser(path, { ...init, signal: controller.signal })
      .then(async response => {
        const body = await response.arrayBuffer()
        return new Response(body, {
          status: response.status,
          statusText: response.statusText,
          headers: response.headers,
        })
      })
      .finally(() => {
        signal.removeEventListener("abort", abort)
        if (timeout !== undefined) clearTimeout(timeout)
      })
  }
}
function preserveAuthFailure(
  outcomes: readonly PromiseSettledResult<unknown>[]
) {
  for (const outcome of outcomes) {
    if (
      outcome.status === "rejected" &&
      outcome.reason instanceof ApiError &&
      outcome.reason.status === 401
    )
      throw outcome.reason
  }
}
const notGenerated = z.object({ code: z.literal("report_not_generated") })
export function marketQueries(scope: MarketScope, locale: Locale) {
  const query = <T>(
    resource: string,
    dependencies: unknown[],
    fn: (transport: ApiTransport) => Promise<T>,
    timeout?: number
  ) => ({
    queryKey: marketQueryKey(scope, resource, ...dependencies),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      fn(marketTransport(signal, timeout)),
  })
  const markets = () =>
    query("visible", [locale], async transport => {
      try {
        return (await createMarketClient(transport).list())
          .filter(m => m.is_visible)
          .map(m => ({ code: m.code, name: marketName(m, locale) }))
      } catch (error) {
        if (error instanceof ApiError && error.status === 403)
          return marketCodeSchema.options.map(code => ({ code, name: code }))
        throw error
      }
    })
  return {
    markets,
    reports: () =>
      query("reports", [locale], async transport => {
        try {
          return (await createReportClient(transport).list(locale)).map(
            mapReportSummary
          )
        } catch (error) {
          if (error instanceof ApiError && error.status === 403) return []
          throw error
        }
      }),
    report: (marketCode: MarketCode) =>
      query("report", [marketCode, locale], async transport => {
        const launched = launchMarketCodeSchema.safeParse(marketCode)
        if (!launched.success)
          return { kind: "not-launched" as const, marketCode }
        try {
          return {
            kind: "report" as const,
            report: mapReportDetail(
              await createReportClient(transport).latest(launched.data, locale)
            ),
          }
        } catch (error) {
          if (error instanceof ApiError && error.status === 404)
            return notGenerated.safeParse(error.detail).success
              ? { kind: "not-generated" as const, marketCode }
              : { kind: "not-found" as const }
          throw error
        }
      }),
    news: (marketCode?: string) =>
      query("news", [marketCode ?? "global", locale], transport => {
        const client = createNewsClient(transport)
        return marketCode && isNewsMarketCode(marketCode)
          ? client.latestForMarket(locale, marketCode)
          : client.latest(locale)
      }),
    viewpoints: () =>
      query("viewpoints", [], transport =>
        createAnalystViewpointClient(transport).today()
      ),
    history: (marketCode: MarketCode, range: { start: string; end: string }) =>
      query(
        "history",
        [marketCode, range],
        async transport => {
          const symbols = indexChartSymbolsForMarket(marketCode)
          const client = createMarketClient(transport)
          const outcomes = await Promise.allSettled(
            symbols.map(symbol => client.indexDailyBars(symbol, range))
          )
          preserveAuthFailure(outcomes)
          if (outcomes.every(outcome => outcome.status === "rejected"))
            throw outcomes.find(outcome => outcome.status === "rejected")
              ?.reason
          return {
            marketCode,
            ...range,
            ...indexHistoryOutcomes(symbols, outcomes),
          }
        },
        10_000
      ),
    averages: (marketCode: MarketCode, range: { start: string; end: string }) =>
      query(
        "averages",
        [marketCode, range],
        async transport => {
          const symbols = indexChartSymbolsForMarket(marketCode)
          const client = createMarketClient(transport)
          const outcomes = await Promise.allSettled(
            symbols.map(symbol => client.indexMovingAverages(symbol, range))
          )
          preserveAuthFailure(outcomes)
          if (outcomes.every(outcome => outcome.status === "rejected"))
            throw outcomes.find(outcome => outcome.status === "rejected")
              ?.reason
          return indexMovingAverageOutcomes(symbols, outcomes)
        },
        10_000
      ),
    vix: (range: { start: string; end: string }) =>
      query(
        "vix",
        [range],
        async transport => ({
          symbol: VIX_SYMBOL,
          ...range,
          bars: await createMarketClient(transport).indexDailyBars(
            VIX_SYMBOL,
            range
          ),
          indicators: null,
        }),
        10_000
      ),
    vixAverages: (range: { start: string; end: string }) =>
      query(
        "vix-averages",
        [range],
        transport =>
          createMarketClient(transport).indexMovingAverages(VIX_SYMBOL, range),
        10_000
      ),
    flows: (range: { start: string; end: string }) =>
      query(
        "flows",
        ["tw_equity", range],
        transport => createMarketClient(transport).institutionalFlows(range),
        10_000
      ),
    stocks: (date: string) =>
      query(
        "stocks",
        ["tw_equity", date, locale],
        transport =>
          createMarketClient(transport).institutionalStocks({ date, locale }),
        10_000
      ),
    macro: () =>
      query(
        "macro",
        ["global_macro_bonds"],
        async transport => {
          const response = await transport(
            "/api/reports/global_macro_bonds/dashboard"
          )
          if (!response.ok)
            throw new ApiError(
              response.status,
              response.headers.get("X-Request-ID"),
              "Macro dashboard unavailable"
            )
          return macroDashboardSchema.parse(await response.json())
        },
        60_000
      ),
  }
}
export function useMarketQuery<T>(
  options: {
    queryKey: readonly unknown[]
    queryFn: (context: { signal: AbortSignal }) => Promise<T>
  },
  locale: Locale,
  enabled = true
) {
  const hydrated = useHydrated()
  const router = useRouter()
  const redirectExpired = useSessionExpiryRedirect(locale, "customer")
  const result = useQuery({
    ...marketQueryPolicy,
    ...options,
    enabled: hydrated && enabled && !isSessionExpiryPending(router),
  })
  useEffect(() => {
    if (result.error) void redirectExpired(result.error)
  }, [redirectExpired, result.error])
  return result
}
