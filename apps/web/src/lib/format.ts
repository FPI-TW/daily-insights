import type { Locale } from "@daily-insights/api-client"

// Display-only formatting for report numbers. Values arrive as Decimal strings
// (see contracts.py: the API avoids locale formatting and binary floating
// point on purpose); this module only rounds for display and never computes.
export const numberLocales: Record<Locale, string> = {
  "zh-hant": "zh-Hant-TW",
  "zh-hans": "zh-Hans-CN",
  en: "en-US",
}

export type Direction = "up" | "down" | "flat" | "none"

const CURRENCY_CODE = /^[a-z]{3}$/
const KNOWN_UNITS = new Set([
  "percent",
  "index",
  "usd_percent",
  "provider_quote_currency",
  "ratio",
])
const warnedUnits = new Set<string>()

function toNumber(raw: string | number): number | null {
  const parsed = typeof raw === "number" ? raw : Number(raw)
  return Number.isFinite(parsed) ? parsed : null
}

function warnUnknownUnit(unit: string) {
  if (!import.meta.env.DEV || warnedUnits.has(unit)) return
  warnedUnits.add(unit)
  console.warn(`Unknown report unit_code "${unit}"; using the default format.`)
}

/** Fraction digits by unit: currencies keep four digits below 10 (ADA at
 * 0.2046 would otherwise lose its meaningful digits). */
function fractionDigits(unit: string, value: number) {
  if (unit === "ratio") return 6
  if (unit === "percent") return 2
  if (CURRENCY_CODE.test(unit)) return Math.abs(value) < 10 ? 4 : 2
  if (!KNOWN_UNITS.has(unit) && unit !== "") warnUnknownUnit(unit)
  return 2
}

/** Format a report value for display. `unitCode` decides precision and the
 * percent suffix; the number itself is untouched apart from rounding. */
export function formatNumber(
  raw: string | number | null | undefined,
  unitCode: string | null | undefined,
  locale: Locale
): string {
  if (raw === null || raw === undefined) return "—"
  const value = toNumber(raw)
  if (value === null) return String(raw)
  const unit = (unitCode ?? "").toLowerCase()
  const digits = fractionDigits(unit, value)
  const text = new Intl.NumberFormat(numberLocales[locale], {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value)
  return unit === "percent" ? `${text}%` : text
}

/** Format a change. Metric changes are always percentage moves (the API fills
 * them from the provider's percent_change regardless of the value's
 * unit_code); table cells pass `percent` from their column's unit_code. Zero
 * is reported as "flat" so it never looks like a real +0.00% or like missing
 * data, which is rendered as an em dash by the caller. */
export function formatChange(
  raw: string | number | null | undefined,
  locale: Locale,
  options: { percent?: boolean; flatLabel: string }
): { text: string; direction: Direction } {
  if (raw === null || raw === undefined) return { text: "—", direction: "none" }
  const value = toNumber(raw)
  if (value === null) {
    return { text: String(raw), direction: literalDirection(String(raw)) }
  }
  if (value === 0) return { text: options.flatLabel, direction: "flat" }
  const percent = options.percent ?? true
  const text = new Intl.NumberFormat(numberLocales[locale], {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    signDisplay: "exceptZero",
  }).format(value)
  return {
    text: percent ? `${text}%` : text,
    direction: value > 0 ? "up" : "down",
  }
}

/** Direction of pre-formatted text (test fixtures and translated values). */
export function literalDirection(text: string): Direction {
  if (text.startsWith("+")) return "up"
  if (text.startsWith("-")) return "down"
  return "none"
}

/** Tailwind colour class per direction. Colour is never the only carrier:
 * the sign is part of the text and flat/none stay neutral. */
export function directionClass(direction: Direction) {
  if (direction === "up") return "text-market-up"
  if (direction === "down") return "text-market-down"
  return "text-sea-ink-soft"
}

const UNIT_LABEL_KEYS: Record<string, string> = {
  usd: "reportUnit_usd",
  eur: "reportUnit_eur",
  percent: "reportUnit_percent",
  index: "reportUnit_index",
  usd_percent: "reportUnit_usd_percent",
  provider_quote_currency: "reportUnit_provider_quote_currency",
}

/** Human label for a unit_code. Unknown currency codes are shown upper-cased;
 * anything else falls back to the raw code so it is at least visible. */
export function unitLabel(
  unitCode: string | null | undefined,
  t: (key: string) => string
): string | null {
  if (!unitCode) return null
  const unit = unitCode.toLowerCase()
  const key = UNIT_LABEL_KEYS[unit]
  if (key) return t(key)
  return CURRENCY_CODE.test(unit) ? unit.toUpperCase() : unitCode
}

/** ISO date (YYYY-MM-DD) to a locale date; pinned to UTC so the calendar day
 * never shifts with the viewer's zone. */
export function formatIsoDate(date: string, locale: Locale): string {
  return new Intl.DateTimeFormat(numberLocales[locale], {
    dateStyle: "medium",
    timeZone: "UTC",
  }).format(new Date(`${date}T00:00:00Z`))
}
