import type { AnalystViewpointSyncStatus } from "@daily-insights/api-client"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"
import { createAdministrationClient } from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"

export const getAnalystViewpointStatus = createServerFn({
  method: "GET",
}).handler(async (): Promise<AnalystViewpointSyncStatus> => {
  setResponseHeader("Cache-Control", "no-store")
  const apiUrl = process.env.API_INTERNAL_URL
  if (!apiUrl) {
    throw new Error("API_INTERNAL_URL is required by the web server")
  }
  const cookie = getRequestHeader("cookie")
  const requestId = getRequestHeader("x-request-id")
  return createAdministrationClient(
    createServerTransport(apiUrl, {
      ...(cookie ? { cookie } : {}),
      ...(requestId ? { requestId } : {}),
    })
  ).analystViewpointStatus()
})
