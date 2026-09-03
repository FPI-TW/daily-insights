import {
  ApiError,
  createMarketClient,
  localeSchema,
  marketCodeSchema,
  type Locale,
  type Market,
  type MarketCode,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"

export type NavMarket = { code: MarketCode; name: string }

export function marketName(market: Market, locale: Locale) {
  if (locale === "zh-hant") return market.name_zh_hant
  if (locale === "zh-hans") return market.name_zh_hans
  return market.name_en
}

// Not exported: anything referencing the server request helpers must stay
// inside this module's server function so the client bundle never sees it.
function serverMarketClient() {
  const apiUrl = process.env.API_INTERNAL_URL
  if (!apiUrl) throw new Error("API_INTERNAL_URL is required by the web server")
  const cookie = getRequestHeader("cookie")
  const requestId = getRequestHeader("x-request-id")
  return createMarketClient(
    createServerTransport(apiUrl, {
      ...(cookie ? { cookie } : {}),
      ...(requestId ? { requestId } : {}),
    })
  )
}

// Internal users have no organization, so /api/markets answers 403 for them;
// they may preview every market, which the full catalog represents. Names
// fall back to the code because every catalog market has a short label.
const catalogMarkets: NavMarket[] = marketCodeSchema.options.map(code => ({
  code,
  name: code,
}))

// Navigation is driven by the organization's visible markets so a market with
// data never becomes unreachable, and new markets appear without a release.
export const getVisibleMarkets = createServerFn({ method: "GET" })
  .validator(localeSchema)
  .handler(async ({ data: locale }): Promise<NavMarket[]> => {
    setResponseHeader("Cache-Control", "no-store")
    try {
      const markets = await serverMarketClient().list()
      return markets
        .filter(market => market.is_visible)
        .map(market => ({
          code: market.code,
          name: marketName(market, locale),
        }))
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) {
        return catalogMarkets
      }
      throw error
    }
  })
