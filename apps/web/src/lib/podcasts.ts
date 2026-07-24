import {
  createBrowserTransport,
  createPodcastClient,
  localeSchema,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"
import { z } from "zod"

const detailInputSchema = z.object({
  episodeId: z.uuid(),
  locale: localeSchema,
})

function serverPodcastClient() {
  const apiUrl = process.env.API_INTERNAL_URL
  if (!apiUrl) {
    throw new Error("API_INTERNAL_URL is required by the web server")
  }
  const cookie = getRequestHeader("cookie")
  const requestId = getRequestHeader("x-request-id")
  return createPodcastClient(
    createServerTransport(apiUrl, {
      ...(cookie ? { cookie } : {}),
      ...(requestId ? { requestId } : {}),
    })
  )
}

export const getPodcastList = createServerFn({ method: "GET" })
  .validator(localeSchema)
  .handler(({ data: locale }) => {
    setResponseHeader("Cache-Control", "no-store")
    return serverPodcastClient().list(locale)
  })

export const getPodcastDetail = createServerFn({ method: "GET" })
  .validator(detailInputSchema)
  .handler(({ data }) => {
    setResponseHeader("Cache-Control", "no-store")
    return serverPodcastClient().get(data.episodeId, data.locale)
  })

export function browserPodcastClient() {
  return createPodcastClient(createBrowserTransport())
}
