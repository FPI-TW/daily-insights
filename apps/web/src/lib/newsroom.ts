import {
  createNewsroomClient,
  localeSchema,
  newsroomMarketCodeSchema,
  type NewsroomEdition,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"
import { z } from "zod"

const newsroomEditionInputSchema = z.object({
  locale: localeSchema,
  marketCode: newsroomMarketCodeSchema,
})

function serverNewsroomClient() {
  const apiUrl = process.env.API_INTERNAL_URL
  if (!apiUrl) throw new Error("API_INTERNAL_URL is required by the web server")
  const cookie = getRequestHeader("cookie")
  const requestId = getRequestHeader("x-request-id")
  return createNewsroomClient(
    createServerTransport(apiUrl, {
      ...(cookie ? { cookie } : {}),
      ...(requestId ? { requestId } : {}),
    })
  )
}

export const getNewsroomEdition = createServerFn({ method: "GET" })
  .validator(newsroomEditionInputSchema)
  .handler(async ({ data }): Promise<NewsroomEdition> => {
    setResponseHeader("Cache-Control", "no-store")
    return serverNewsroomClient().latestEdition(data.marketCode, data.locale)
  })
