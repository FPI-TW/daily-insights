import {
  createNewsClient,
  localeSchema,
  type LatestNews,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"

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
