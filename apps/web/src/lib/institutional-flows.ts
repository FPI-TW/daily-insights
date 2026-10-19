import type {
  InstitutionalFlows,
  InstitutionalStocks,
} from "@daily-insights/api-client"
export function institutionalFlowRange(end: string) {
  const endDate = new Date(`${end}T00:00:00Z`)
  endDate.setUTCDate(endDate.getUTCDate() - 100)
  return { start: endDate.toISOString().slice(0, 10), end }
}

export type TaiwanInstitutionalData = {
  flows: InstitutionalFlows | null
  stocks: InstitutionalStocks | null
}
