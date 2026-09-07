import { z } from "zod"

const decimal = z
  .string()
  .regex(/^-?\d+(\.\d+)?$/)
  .refine(value => Number.isFinite(Number(value)))
export const macroHistorySchema = z.object({
  id: z.string(),
  symbol: z.string(),
  unit: z.string(),
  source: z.string(),
  status: z.enum(["ok", "unavailable", "disabled"]),
  points: z.array(z.object({ date: z.iso.date(), value: decimal })),
})
export const macroDashboardSchema = z.object({
  fetched_at: z.iso.datetime({ offset: true }),
  histories: z.array(macroHistorySchema),
  calendar: z.object({
    date: z.iso.date(),
    status: z.enum(["ok", "unavailable", "disabled"]),
    events: z.array(
      z.object({
        date: z.iso.datetime({ offset: true }),
        country: z.string(),
        event: z.string(),
        currency: z.string().nullable(),
        impact: z.string().nullable(),
        estimate: decimal.nullable(),
        previous: decimal.nullable(),
        actual: decimal.nullable(),
        unit: z.string().nullable(),
      })
    ),
  }),
})
export type MacroDashboardData = z.infer<typeof macroDashboardSchema>
export type MacroHistory = z.infer<typeof macroHistorySchema>
export type Period = "day" | "week" | "month" | "year"

// Calendar periods, using the nearest earlier observation within seven days.
// Daily changes use the previous available trading session. Missing history
// stays missing instead of silently using the first available price.
export function periodChange(
  history: MacroHistory,
  period: Period,
  basisPoints = false
) {
  const latest = history.points.at(-1)
  if (!latest) return null
  const date = new Date(`${latest.date}T00:00:00Z`)
  if (period === "week") date.setUTCDate(date.getUTCDate() - 7)
  if (period === "month" || period === "year") {
    const day = date.getUTCDate()
    date.setUTCDate(1)
    if (period === "month") date.setUTCMonth(date.getUTCMonth() - 1)
    else date.setUTCFullYear(date.getUTCFullYear() - 1)
    const lastDay = new Date(
      Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)
    ).getUTCDate()
    date.setUTCDate(Math.min(day, lastDay))
  }
  const target = date.toISOString().slice(0, 10)
  const previous =
    period === "day"
      ? history.points.at(-2)
      : [...history.points].reverse().find(point => point.date <= target)
  if (!previous) return null
  if (
    period !== "day" &&
    date.getTime() - Date.parse(previous.date) > 7 * 86_400_000
  )
    return null
  const start = Number(previous.value)
  const end = Number(latest.value)
  return basisPoints
    ? (end - start) * 100
    : start === 0
      ? null
      : (end / start - 1) * 100
}

export function ratioPoints(
  numerator: MacroHistory | undefined,
  denominator: MacroHistory | undefined
) {
  const byDate = new Map(
    denominator?.points.map(point => [point.date, Number(point.value)])
  )
  return (numerator?.points ?? []).flatMap(point => {
    const divisor = byDate.get(point.date)
    return divisor && divisor > 0
      ? [{ date: point.date, value: Number(point.value) / divisor }]
      : []
  })
}

export function recentPoints(history: MacroHistory | undefined, days: number) {
  const latest = history?.points.at(-1)
  if (!history || !latest) return []
  const start = Date.parse(latest.date) - (days - 1) * 86_400_000
  return history.points.filter(point => Date.parse(point.date) >= start)
}

export function formatTaipeiTimestamp(value: string) {
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(value))
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find(item => item.type === type)?.value ?? ""
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}`
}
