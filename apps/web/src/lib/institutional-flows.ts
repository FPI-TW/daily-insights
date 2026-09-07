import {
  createMarketClient,
  type InstitutionalMarketFlow,
  type InstitutionalStockFlowLeaders,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"
import { z } from "zod"

const flowRequestSchema = z.object({
  startDate: z.iso.date(),
  endDate: z.iso.date(),
})
const stockRequestSchema = z.object({
  tradeDate: z.iso.date().optional(),
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
  .handler(async ({ data }): Promise<InstitutionalMarketFlow[]> => {
    setResponseHeader("Cache-Control", "no-store")
    return serverMarketClient().institutionalMarketFlows(data)
  })

export const getTaiwanInstitutionalStocks = createServerFn({ method: "GET" })
  .validator(stockRequestSchema)
  .handler(async ({ data }): Promise<InstitutionalStockFlowLeaders> => {
    setResponseHeader("Cache-Control", "no-store")
    return serverMarketClient().institutionalStockFlowLeaders(data.tradeDate)
  })

export function institutionalFlowRange(end: string) {
  const endDate = new Date(`${end}T00:00:00Z`)
  endDate.setUTCDate(endDate.getUTCDate() - 100)
  return { startDate: endDate.toISOString().slice(0, 10), endDate: end }
}

export type TaiwanInstitutionalData = {
  flows: InstitutionalMarketFlow[] | null
  stocks: InstitutionalStockFlowLeaders | null
}
