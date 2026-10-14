import {
  trackedIndexCatalog,
  type IndexDailyBar,
  type IndexMovingAverages,
  type MarketCode,
} from "@daily-insights/api-client"
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

/** Volatility indices get their own banded chart instead of the index chart. */
export const volatilitySymbols = ["^VIX", "^HSIL"] as const
export type VolatilitySymbol = (typeof volatilitySymbols)[number]

export type VixHistory = {
  symbol: VolatilitySymbol
  start: string
  end: string
  bars: IndexDailyBar[]
  indicators: IndexMovingAverages | null
}

export type IndexMovingAverageMap = Record<string, IndexMovingAverages>

export function trackedSymbolsForMarket(marketCode: MarketCode) {
  return trackedIndexCatalog
    .filter(item => item.marketCode === marketCode)
    .map(item => item.symbol)
}

export function indexChartSymbolsForMarket(marketCode: MarketCode) {
  return trackedSymbolsForMarket(marketCode).filter(
    symbol => !volatilitySymbols.some(volatility => volatility === symbol)
  )
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

export function indexNameKey(symbol: string) {
  return (
    {
      "^DJI": "indexNameDji",
      "^GSPC": "indexNameGspc",
      "^NDX": "indexNameNdx",
      "^RUT": "indexNameRut",
      "^SOX": "indexNameSox",
      "^VIX": "indexNameVix",
      "^HSI": "indexNameHsi",
      "^HSCE": "indexNameHsce",
      "^HSIL": "indexNameHsil",
      "^TWII": "indexNameTwii",
    } satisfies Record<string, string>
  )[symbol]
}

function finiteNumber(raw: string | number | null | undefined) {
  if (raw == null) return null
  const value = Number(raw)
  return Number.isFinite(value) ? value : null
}

/** Points moved from `baseline` to `close`, or null when either is missing
 * or non-numeric. */
export function pointChange(
  close: string | number | null | undefined,
  baseline: string | number | null | undefined
): number | null {
  const latest = finiteNumber(close)
  const previous = finiteNumber(baseline)
  if (latest === null || previous === null) return null
  return latest - previous
}

/** Percent move from `baseline` to `close`; also null for a zero baseline. */
export function percentageChange(
  close: string | number | null | undefined,
  baseline: string | number | null | undefined
): number | null {
  const points = pointChange(close, baseline)
  const previous = finiteNumber(baseline)
  if (points === null || previous === null || previous === 0) return null
  return (points / previous) * 100
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
