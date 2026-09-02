import {
  createNewsClient,
  localeSchema,
  newsMarketCodeSchema,
  type LatestNews,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"
import { z } from "zod"

const marketNewsInputSchema = z.object({
  locale: localeSchema,
  marketCode: newsMarketCodeSchema,
})

function serverNewsClient() {
  const apiUrl = process.env.API_INTERNAL_URL
  if (!apiUrl) throw new Error("API_INTERNAL_URL is required by the web server")
  const cookie = getRequestHeader("cookie")
  const requestId = getRequestHeader("x-request-id")
  return createNewsClient(
    createServerTransport(apiUrl, {
      ...(cookie ? { cookie } : {}),
      ...(requestId ? { requestId } : {}),
    })
  )
}

export const getLatestNews = createServerFn({ method: "GET" })
  .validator(localeSchema)
  .handler(async ({ data: locale }): Promise<LatestNews> => {
    setResponseHeader("Cache-Control", "no-store")
    return serverNewsClient().latest(locale)
  })

export const getMarketNews = createServerFn({ method: "GET" })
  .validator(marketNewsInputSchema)
  .handler(async ({ data }): Promise<LatestNews> => {
    setResponseHeader("Cache-Control", "no-store")
    return serverNewsClient().latestForMarket(data.locale, data.marketCode)
  })
