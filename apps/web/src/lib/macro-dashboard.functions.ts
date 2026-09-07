import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"
import { macroDashboardSchema } from "./macro-dashboard"

export const getMacroDashboard = createServerFn({ method: "GET" }).handler(
  async () => {
    setResponseHeader("Cache-Control", "no-store")
    const apiUrl = process.env.API_INTERNAL_URL
    if (!apiUrl) throw new Error("API_INTERNAL_URL is required")
    const cookie = getRequestHeader("cookie")
    const transport = createServerTransport(apiUrl, cookie ? { cookie } : {})
    // Authorization is enforced by the API before accessing shared market data.
    const response = await transport(
      "/api/reports/global_macro_bonds/dashboard",
      {
        signal: AbortSignal.timeout(60_000),
      }
    )
    if (!response.ok) throw new Error("Macro dashboard is unavailable")
    return macroDashboardSchema.parse(await response.json())
  }
)
