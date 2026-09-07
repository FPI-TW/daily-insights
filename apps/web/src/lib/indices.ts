import {
  createMarketClient,
  marketCodeSchema,
  trackedIndexCatalog,
  type IndexDailyBar,
  type IndexMovingAverages,
  type MarketCode,
} from "@daily-insights/api-client"
import { createServerTransport } from "@daily-insights/api-client/server"
import { createServerFn } from "@tanstack/react-start"
import {
  getRequestHeader,
  setResponseHeader,
} from "@tanstack/react-start/server"
import { z } from "zod"

export type IndexHistorySeries = {
  symbol: string
  bars: IndexDailyBar[]
}

export type MarketIndexHistory = {
  marketCode: MarketCode
  start: string
  end: string
  series: IndexHistorySeries[]
  failedSymbols: string[]
}

export type IndexMovingAverageMap = Record<string, IndexMovingAverages>

const marketIndexRequestSchema = z.object({
  marketCode: marketCodeSchema,
  range: z.object({ start: z.iso.date(), end: z.iso.date() }).optional(),
})

export const chartMarketCodes = ["us_equity", "tw_equity"] as const

export function trackedSymbolsForMarket(marketCode: MarketCode) {
  return trackedIndexCatalog
    .filter(item => item.marketCode === marketCode)
    .map(item => item.symbol)
}

export function indexHistoryOutcomes(
  symbols: readonly string[],
  outcomes: readonly PromiseSettledResult<IndexDailyBar[]>[]
) {
  return {
    series: outcomes.flatMap((outcome, index) =>
      outcome.status === "fulfilled" && outcome.value.length > 0
        ? [{ symbol: symbols[index]!, bars: outcome.value }]
        : []
    ),
    failedSymbols: outcomes.flatMap((outcome, index) =>
      outcome.status === "rejected" || outcome.value.length === 0
        ? [symbols[index]!]
        : []
    ),
  }
}

export function indexMovingAverageOutcomes(
  symbols: readonly string[],
  outcomes: readonly PromiseSettledResult<IndexMovingAverages>[]
): IndexMovingAverageMap {
  return Object.fromEntries(
    outcomes.flatMap((outcome, index) => {
      if (
        outcome.status !== "fulfilled" ||
        !outcome.value.series.some(series =>
          series.points.some(point => point.value !== null)
        )
      ) {
        return []
      }
      return [[symbols[index]!, outcome.value]]
    })
  )
}

function taipeiDateParts() {
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date())
  const value = (type: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find(part => part.type === type)?.value)
  return { year: value("year"), month: value("month"), day: value("day") }
}

function isoDate(year: number, month: number, day: number) {
  return `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`
}

export function twoYearTaipeiRange(now = taipeiDateParts()) {
  const end = isoDate(now.year, now.month, now.day)
  const targetYear = now.year - 2
  const lastDay = new Date(Date.UTC(targetYear, now.month, 0)).getUTCDate()
  const start = isoDate(targetYear, now.month, Math.min(now.day, lastDay))
  return { start, end }
}

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

export const getMarketIndexHistory = createServerFn({ method: "GET" })
  .validator(marketIndexRequestSchema)
  .handler(async ({ data }): Promise<MarketIndexHistory> => {
    setResponseHeader("Cache-Control", "no-store")
    const range = data.range ?? twoYearTaipeiRange()
    if (
      !(chartMarketCodes as readonly MarketCode[]).includes(data.marketCode)
    ) {
      return {
        marketCode: data.marketCode,
        ...range,
        series: [],
        failedSymbols: [],
      }
    }

    const client = serverMarketClient()
    const symbols = trackedSymbolsForMarket(data.marketCode)
    const outcomes = await Promise.allSettled(
      symbols.map(symbol => client.indexDailyBars(symbol, range))
    )

    return {
      marketCode: data.marketCode,
      ...range,
      ...indexHistoryOutcomes(symbols, outcomes),
    }
  })

export const getMarketIndexMovingAverages = createServerFn({ method: "GET" })
  .validator(marketIndexRequestSchema)
  .handler(async ({ data }): Promise<IndexMovingAverageMap> => {
    setResponseHeader("Cache-Control", "no-store")
    if (
      !(chartMarketCodes as readonly MarketCode[]).includes(data.marketCode)
    ) {
      return {}
    }
    const range = data.range ?? twoYearTaipeiRange()
    const client = serverMarketClient()
    const symbols = trackedSymbolsForMarket(data.marketCode)
    const outcomes = await Promise.allSettled(
      symbols.map(symbol => client.indexMovingAverages(symbol, range))
    )
    return indexMovingAverageOutcomes(symbols, outcomes)
  })

export function indexNameKey(symbol: string) {
  return (
    {
      "^DJI": "indexNameDji",
      "^GSPC": "indexNameGspc",
      "^IXIC": "indexNameIxic",
      "^RUT": "indexNameRut",
      "^SOX": "indexNameSox",
      "^TWII": "indexNameTwii",
    } satisfies Record<string, string>
  )[symbol]
}

export const biasPeriods = [20, 60, 120] as const
export type BiasPoint = { date: string; value: number | null }

/** Join by trading day; use the API's sma-close-v1 values, never a local SMA. */
export function biasSeries(
  bars: readonly IndexDailyBar[],
  averages: IndexMovingAverages | undefined
) {
  return biasPeriods.map(period => {
    const byDate = new Map(
      averages?.series
        .find(series => series.period === period)
        ?.points.map(point => [point.trade_date, point.value])
    )
    return {
      period,
      points: bars.map(bar => {
        const ma = byDate.get(bar.trade_date)
        return {
          date: bar.trade_date,
          value:
            ma == null || Number(ma) === 0
              ? null
              : (Number(bar.close) / Number(ma) - 1) * 100,
        }
      }),
    }
  })
}

/** Position of the final visible session among non-null values in that window. */
export function scaleBias(points: readonly BiasPoint[]) {
  const values = points.flatMap(point =>
    point.value === null ? [] : [point.value]
  )
  const current = points.at(-1)?.value ?? null
  const min = values.length ? Math.min(...values) : null
  const max = values.length ? Math.max(...values) : null
  return {
    current,
    min,
    max,
    value:
      current === null || min === null || max === null
        ? null
        : max === min
          ? 50
          : (100 * (current - min)) / (max - min),
  }
}

/** Calendar-month windows clamp month ends instead of rolling into the next month. */
export function indexWindowStart(end: string, months: number) {
  const date = new Date(`${end}T00:00:00Z`)
  const day = date.getUTCDate()
  date.setUTCDate(1)
  date.setUTCMonth(date.getUTCMonth() - months)
  const last = new Date(
    Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)
  ).getUTCDate()
  date.setUTCDate(Math.min(day, last))
  return date.toISOString().slice(0, 10)
}

/** ECharts category zoom uses rounded category indices at each endpoint. */
export function visibleBiasPoints(
  points: readonly BiasPoint[],
  start: number,
  end: number
) {
  const last = Math.max(0, points.length - 1)
  return points.slice(
    Math.round((last * start) / 100),
    Math.round((last * end) / 100) + 1
  )
}
