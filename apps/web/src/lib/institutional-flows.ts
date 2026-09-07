import {
  createMarketClient,
  localeSchema,
  type InstitutionalFlows,
  type InstitutionalStocks,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"
import { z } from "zod"

const flowRequestSchema = z.object({
  start: z.iso.date(),
  end: z.iso.date(),
})
const stockRequestSchema = z.object({
  date: z.iso.date(),
  locale: localeSchema,
})

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

export const getTaiwanInstitutionalFlows = createServerFn({ method: "GET" })
  .validator(flowRequestSchema)
  .handler(async ({ data }): Promise<InstitutionalFlows> => {
    setResponseHeader("Cache-Control", "no-store")
    return serverMarketClient().institutionalFlows(data)
  })

export const getTaiwanInstitutionalStocks = createServerFn({ method: "GET" })
  .validator(stockRequestSchema)
  .handler(async ({ data }): Promise<InstitutionalStocks> => {
    setResponseHeader("Cache-Control", "no-store")
    return serverMarketClient().institutionalStocks(data)
  })

export function institutionalFlowRange(end: string) {
  const endDate = new Date(`${end}T00:00:00Z`)
  endDate.setUTCDate(endDate.getUTCDate() - 100)
  return { start: endDate.toISOString().slice(0, 10), end }
}

export type TaiwanInstitutionalData = {
  flows: InstitutionalFlows | null
  stocks: InstitutionalStocks | null
}
