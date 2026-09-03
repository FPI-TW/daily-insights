import {
  createAnalystViewpointClient,
  type AnalystViewpoint,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"

export const getTodayAnalystViewpoints = createServerFn({
  method: "GET",
}).handler(async (): Promise<AnalystViewpoint[]> => {
  setResponseHeader("Cache-Control", "no-store")
  const apiUrl = process.env.API_INTERNAL_URL
  if (!apiUrl) {
    throw new Error("API_INTERNAL_URL is required by the web server")
  }
  const cookie = getRequestHeader("cookie")
  const requestId = getRequestHeader("x-request-id")
  return createAnalystViewpointClient(
    createServerTransport(apiUrl, {
      ...(cookie ? { cookie } : {}),
      ...(requestId ? { requestId } : {}),
    })
  ).today()
})
