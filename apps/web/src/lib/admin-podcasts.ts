import {
  createBrowserTransport,
  createPodcastAdminClient,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"

export const getAdminPodcastList = createServerFn({ method: "GET" }).handler(
  () => {
    setResponseHeader("Cache-Control", "no-store")
    const apiUrl = process.env.API_INTERNAL_URL
    if (!apiUrl) {
      throw new Error("API_INTERNAL_URL is required by the web server")
    }
    const cookie = getRequestHeader("cookie")
    const requestId = getRequestHeader("x-request-id")
    return createPodcastAdminClient(
      createServerTransport(apiUrl, {
        ...(cookie ? { cookie } : {}),
        ...(requestId ? { requestId } : {}),
      })
    ).list()
  }
)

export function browserPodcastAdminClient() {
  return createPodcastAdminClient(createBrowserTransport())
}
